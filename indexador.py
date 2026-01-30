import os
import hashlib
import time
import logging
import traceback
import resend
import urllib3
import io
import gc
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from pymongo import MongoClient
from opensearchpy import OpenSearch, helpers
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from tqdm import tqdm
import pytesseract
from pdf2image import convert_from_path
import pdfplumber
from tenacity import retry, stop_after_attempt, wait_exponential

# ==================================================
# 1️⃣ ENV e CONFIGURAÇÃO
# ==================================================
load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PASTAS_DOCS = [p.strip() for p in os.getenv("PASTA_DOCUMENTOS", "").split(";") if p.strip()]
MONGO_URI = os.getenv("MONGO_URI")
OS_PROTOCOL = os.getenv("OS_PROTOCOL", "http")
OS_URL = f"{OS_PROTOCOL}://{os.getenv('OS_HOST')}:{os.getenv('OS_PORT')}"
OS_AUTH = (os.getenv("OS_USER"), os.getenv("OS_PASS"))
OS_INDEX = os.getenv("OS_INDEX")
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
resend.api_key = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_SENDER")
EMAILS_TO = os.getenv("EMAIL_TO").split(",")
EMAILS_CC = os.getenv("EMAIL_CC").split(",")

# ==================================================
# 2️⃣ LOGGING
# ==================================================
def configurar_logger():
    log_stream = io.StringIO()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = logging.FileHandler("index.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    memory_handler = logging.StreamHandler(log_stream)
    memory_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not any(isinstance(h, logging.FileHandler) for h in root_logger.handlers):
        root_logger.addHandler(file_handler)
    if not any(isinstance(h, logging.StreamHandler) and h.stream is log_stream for h in root_logger.handlers):
        root_logger.addHandler(memory_handler)

    logging.captureWarnings(True)
    return log_stream

# ==================================================
# 3️⃣ FUNÇÕES AUXILIARES
# ==================================================
def enviar_notificacao(assunto, html, caminho_log_arquivo=None):
    try:
        email_data = {
            "from": f"Sistema Jurídico <{EMAIL_FROM}>",
            "to": EMAILS_TO,
            "cc": EMAILS_CC,
            "subject": assunto,
            "html": html
        }
        if caminho_log_arquivo and os.path.exists(caminho_log_arquivo):
            with open(caminho_log_arquivo, "rb") as f:
                encoded_log = base64.b64encode(f.read()).decode("utf-8")
            email_data["attachments"] = [{"filename": "logs.txt", "content": encoded_log}]
        resend.Emails.send(email_data)
        logging.info("E-mail enviado com sucesso.")
    except Exception as e:
        logging.error(f"Erro no envio de e-mail: {e}")

def calcular_hash(caminho):
    sha = hashlib.sha256()
    with open(caminho, "rb") as f:
        while chunk := f.read(8192):
            sha.update(chunk)
    return sha.hexdigest()

def normalizar_caminho(caminho):
    return os.path.abspath(caminho)

def extrair_conteudo(caminho):
    paginas = []
    try:
        with pdfplumber.open(caminho) as pdf:
            for i, p in enumerate(pdf.pages, 1):
                texto = (p.extract_text() or "").strip()
                if len(texto) < 50:
                    try:
                        imagens = convert_from_path(caminho, dpi=OCR_DPI, first_page=i, last_page=i)
                        if imagens:
                            ocr_texto = (pytesseract.image_to_string(imagens[0], lang="por") or "").strip()
                            texto = ocr_texto
                            del imagens[0]; del imagens; gc.collect()
                    except Exception as ocr_err:
                        logging.warning(f"Erro OCR página {i} em {os.path.basename(caminho)}: {ocr_err}")
                if texto:
                    paginas.append({"texto": texto, "pagina": i})
    except Exception as e:
        logging.error(f"Erro ao abrir/processar {os.path.basename(caminho)}: {type(e).__name__}: {str(e)}")
    return paginas

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def criar_cliente_opensearch():
    try:
        client = OpenSearch(
            hosts=[OS_URL],
            http_auth=OS_AUTH,
            use_ssl=(OS_PROTOCOL=="https"),
            verify_certs=False,
            ssl_show_warn=False,
            timeout=30,
            max_retries=3
        )
        client.info()
        logging.info("Conexão com OpenSearch estabelecida")
        return client
    except OpenSearchConnectionError as e:
        logging.error(f"Falha ao conectar ao OpenSearch: {e}")
        raise

# ==================================================
# 4️⃣ WORKER SEGURO PARA THREADS
# ==================================================
def processar_arquivo(caminho, mongo_uri):
    """Worker seguro: cria conexão própria com MongoDB"""
    m_client = MongoClient(mongo_uri)
    colecao = m_client["juridico_ocr"]["arquivos"]
    try:
        caminho_abs = os.path.abspath(caminho)
        arquivo_nome = os.path.basename(caminho_abs)
        caminho_corrigido = normalizar_caminho(caminho_abs)
        h = calcular_hash(caminho)

        # 🔹 BLOCO NOVO: Verificação de hash **antes de abrir PDF**
        if colecao.find_one({"hash": h}):
            return (False, h, arquivo_nome, [], caminho_corrigido, "Já indexado")

        paginas = extrair_conteudo(caminho)
        if not paginas:
            return (False, h, arquivo_nome, [], caminho_corrigido, "Sem conteúdo")

        return (True, h, arquivo_nome, paginas, caminho_corrigido, None)
    except Exception as e:
        return (False, None, os.path.basename(caminho), [], None, str(e))

# ==================================================
# 5️⃣ FUNÇÃO PRINCIPAL (incremental)
# ==================================================
def executar():
    logging.info("Iniciando execução incremental do indexador")
    m_client = MongoClient(MONGO_URI)
    colecao = m_client["juridico_ocr"]["arquivos"]
    os_client = criar_cliente_opensearch()

    # 🔹 BLOCO NOVO: Filtra PDFs já indexados antes de processar
    arquivos = []
    for pasta in PASTAS_DOCS:
        if not os.path.exists(pasta): continue
        for root, _, files in os.walk(pasta):
            for f in files:
                if f.lower().endswith(".pdf"):
                    caminho = os.path.join(root, f)
                    h = calcular_hash(caminho)
                    if not colecao.find_one({"hash": h}):
                        arquivos.append(caminho)

    total_arquivos = len(arquivos)
    logging.info(f"Total de PDFs novos para processar: {total_arquivos}")
    if total_arquivos == 0: return 0, [], []

    contador, buffer, novos_hashes, erros = 0, [], set(), []

    def flush_buffer():
        if buffer:
            helpers.bulk(os_client, buffer)
            buffer.clear()
        if novos_hashes:
            colecao.insert_many([{"hash": h} for h in novos_hashes])
            novos_hashes.clear()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(processar_arquivo, caminho, MONGO_URI): caminho for caminho in arquivos}

        with tqdm(total=total_arquivos, desc="Processando PDFs") as pbar:
            for future in as_completed(futures):
                try:
                    sucesso, h, arquivo_nome, paginas, caminho_corrigido, erro = future.result()
                    if sucesso and paginas:
                        novos_hashes.add(h)
                        contador += 1
                        for pagina in paginas:
                            buffer.append({
                                "_index": OS_INDEX,
                                "_id": f"{h}_{pagina['pagina']}",
                                "hash": h,
                                "arquivo": arquivo_nome,
                                "conteudo": pagina["texto"],
                                "pagina": pagina["pagina"],
                                "caminho_original": caminho_corrigido,
                                "data": time.ctime()
                            })
                            if len(buffer) >= 100: flush_buffer()
                    elif erro: erros.append(f"{arquivo_nome}: {erro}")
                except Exception as e: erros.append(f"Erro future: {str(e)}")
                pbar.update(1)

    flush_buffer()
    logging.info(f"Processamento incremental concluído: {contador} PDF(s) indexado(s)")

    resumo_pastas = [(pasta, sum(1 for f in os.listdir(pasta) if f.lower().endswith(".pdf"))) for pasta in PASTAS_DOCS]
    return contador, resumo_pastas, erros

# ==================================================
# 6️⃣ EXECUÇÃO
# ==================================================
if __name__ == "__main__":
    log_stream = configurar_logger()
    start = time.time()
    logging.info("INDEXADOR JURÍDICO INCREMENTAL - INICIANDO")
    try:
        total, resumo_pastas, erros = executar()
        tempo = (time.time() - start)/60

        resumo_html = "<ul>" + "".join([f"<li>{p}: {q} PDFs</li>" for p,q in resumo_pastas]) + "</ul>"
        erros_html = f"<p><b>Erros/Avisos ({len(erros)}):</b></p><ul>" + "".join([f"<li>{e}</li>" for e in erros[:10]]) + \
                     ("</ul><p>... e %d mais</p>" % (len(erros)-10) if len(erros)>10 else "</ul>") if erros else ""

        enviar_notificacao(
            f"✅ Sucesso: {total} novos PDFs indexados",
            f"<h3>Relatório Incremental</h3>"
            f"<p><b>Tempo Total:</b> {tempo:.2f} min</p>"
            f"<p><b>Novos Documentos:</b> {total}</p>"
            f"<p><b>Pastas processadas:</b>{resumo_html}</p>"
            f"{erros_html}"
            f"<p><i>Logs anexados (index.log)</i></p>",
            caminho_log_arquivo="index.log"
        )
        logging.info("E-mail de sucesso enviado")
    except Exception as e:
        logging.error(f"ERRO CRÍTICO: {type(e).__name__}: {str(e)}\n{traceback.format_exc()}")
        enviar_notificacao(
            "❌ Erro no Indexador",
            f"<h3>Falha Crítica Detectada</h3>"
            f"<p><b>Tipo de Erro:</b> {type(e).__name__}</p>"
            f"<p><b>Mensagem:</b> {str(e)}</p>"
            f"<p><i>Logs anexados (index.log)</i></p>",
            caminho_log_arquivo="index.log"
        )
        raise
