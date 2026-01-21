import os
import hashlib
import time
import logging
import traceback
import resend
import urllib3
import io
from dotenv import load_dotenv 
from pymongo import MongoClient
from opensearchpy import OpenSearch, helpers
from tqdm import tqdm
import pytesseract
from pdf2image import convert_from_path
import pdfplumber

# 1. CARREGAR VARIÁVEIS DO FICHEIRO .ENV
load_dotenv()

# Silenciar avisos de SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==================================================
# CONFIGURAÇÕES (LIDAS DO AMBIENTE)
# ==================================================
PASTAS_DOCS_RAW = os.getenv("PASTA_DOCUMENTOS", "")
PASTAS_DOCS = [p.strip() for p in PASTAS_DOCS_RAW.split(";") if p.strip()]

MONGO_URI = os.getenv("MONGO_URI")

# OpenSearch
OS_PROTOCOL = os.getenv("OS_PROTOCOL", "http") # Padrão para http se não existir no .env
OS_URL = f"{OS_PROTOCOL}://{os.getenv('OS_HOST')}:{os.getenv('OS_PORT')}"
OS_AUTH = (os.getenv("OS_USER"), os.getenv("OS_PASS"))
OS_INDEX = os.getenv("OS_INDEX")

# Resend
resend.api_key = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_SENDER")
EMAILS_TO = os.getenv("EMAIL_TO").split(",")
EMAILS_CC = os.getenv("EMAIL_CC").split(",")

# ==================================================
# SISTEMA DE NOTIFICAÇÕES
# ==================================================

def enviar_notificacao(assunto, html):
    try:
        logging.info(f"Enviando e-mail com assunto: {assunto}")
        resend.Emails.send({
            "from": f"Sistema Jurídico <{EMAIL_FROM}>",
            "to": EMAILS_TO,
            "cc": EMAILS_CC,
            "subject": assunto,
            "html": html
        })
        logging.info("E-mail enviado com sucesso.")
    except Exception as e:
        logging.error(f"Erro no e-mail: {e}")

# ==================================================
# LOGGING
# ==================================================
def configurar_logger():
    """Cria logger que escreve em arquivo e também mantém cópia em memória."""
    log_stream = io.StringIO()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler("index.log", encoding='utf-8')
    file_handler.setFormatter(formatter)

    memory_handler = logging.StreamHandler(log_stream)
    memory_handler.setFormatter(formatter)

    # Configura o logger raiz para capturar apenas INFO ou superior
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Evita adicionar handlers duplicados em execuções sucessivas
    if not any(isinstance(h, logging.FileHandler) for h in root_logger.handlers):
        root_logger.addHandler(file_handler)
    if not any(isinstance(h, logging.StreamHandler) and h.stream is log_stream for h in root_logger.handlers):
        root_logger.addHandler(memory_handler)
    
    # Captura warnings do Python também
    logging.captureWarnings(True)
    
    return log_stream

# ==================================================
# FUNÇÕES TÉCNICAS
# ==================================================

def calcular_hash(caminho):
    sha = hashlib.sha256()
    with open(caminho, "rb") as f:
        while chunk := f.read(8192):
            sha.update(chunk)
    return sha.hexdigest()


def normalizar_caminho(caminho):
    caminho_abs = os.path.abspath(caminho)
    if caminho_abs.startswith("/dados"):
        return caminho_abs.replace("/dados", "//10.130.1.99/DeptosMatriz/Juridico", 1)
    return caminho_abs

def extrair_conteudo(caminho):
    paginas = []
    try:
        with pdfplumber.open(caminho) as pdf:
            for i, p in enumerate(pdf.pages, 1):
                texto = (p.extract_text() or "").strip()

                if len(texto) < 50:
                    try:
                        imagens = convert_from_path(caminho, dpi=200, first_page=i, last_page=i)
                        if imagens:
                            ocr_texto = (pytesseract.image_to_string(imagens[0], lang="por") or "").strip()
                            texto = ocr_texto
                    except Exception as ocr_err:
                        logging.warning(f"Erro OCR na página {i} de {os.path.basename(caminho)}: {ocr_err}")

                if texto:
                    paginas.append({"texto": texto, "pagina": i})
    except Exception as e:
        logging.warning(f"Erro na extração de {os.path.basename(caminho)}: {e}")
    return paginas

# ==================================================
# LÓGICA PRINCIPAL
# ==================================================

def executar():
    logging.info("Iniciando conexão com MongoDB e OpenSearch")
    m_client = MongoClient(MONGO_URI)
    colecao = m_client["juridico_ocr"]["arquivos"]
    
    os_client = OpenSearch(
        hosts=[OS_URL],
        http_auth=OS_AUTH,
        use_ssl=(OS_PROTOCOL == "https"), # Só ativa SSL se o protocolo for https
        verify_certs=False, 
        ssl_show_warn=False
    )

    arquivos = []
    resumo_pastas = []
    logging.info(f"Iniciando varredura em {len(PASTAS_DOCS)} pasta(s)")
    
    for pasta in PASTAS_DOCS:
        pasta_abs = os.path.abspath(pasta)
        if os.path.exists(pasta):
            contador_pasta = 0
            
            # Percorre recursivamente todas as subpastas
            for root, dirs, files in os.walk(pasta_abs):
                pdfs_nesta_pasta = [os.path.join(root, f) for f in files if f.lower().endswith('.pdf')]
                if pdfs_nesta_pasta:
                    contador_pasta += len(pdfs_nesta_pasta)
                    arquivos.extend(pdfs_nesta_pasta)
            
            if contador_pasta == 0:
                logging.warning(f"Nenhum PDF encontrado em {pasta_abs}")
            else:
                logging.info(f"Encontrados {contador_pasta} PDF(s) em {pasta_abs}")
            resumo_pastas.append((pasta_abs, contador_pasta))
        else:
            logging.warning(f"Caminho não encontrado ou inacessível: {pasta}")
            resumo_pastas.append((f"{pasta_abs} (inacessível)", 0))

    contador = 0
    buffer = []
    novos_hashes = set()
    total_arquivos = len(arquivos)
    
    logging.info(f"Total de PDF(s) encontrado(s): {total_arquivos}")
    if total_arquivos == 0:
        logging.warning("Nenhum arquivo PDF encontrado. Finalizando...")
        return 0, resumo_pastas
    logging.info(f"Iniciando processamento de {total_arquivos} arquivo(s)...")

    def flush_buffer():
        if buffer:
            helpers.bulk(os_client, buffer)
            buffer.clear()
        if novos_hashes:
            colecao.insert_many([{"hash": h} for h in novos_hashes])
            novos_hashes.clear()

    for idx, caminho in enumerate(arquivos, 1):
        caminho_completo = os.path.abspath(caminho)
        caminho_corrigido = normalizar_caminho(caminho_completo)
        arquivo_nome = os.path.basename(caminho_completo)
        
        try:
            h = calcular_hash(caminho)
            
            # Verifica se já existe no MongoDB
            if colecao.find_one({"hash": h}):
                continue

            paginas = extrair_conteudo(caminho)

            if not paginas:
                logging.warning(f"[{idx}/{total_arquivos}] Arquivo sem conteúdo: {arquivo_nome}")
                continue

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

                if len(buffer) >= 100:
                    flush_buffer()
            
        except Exception as e:
            logging.error(f"[{idx}/{total_arquivos}] Erro ao processar {arquivo_nome}: {str(e)}")
            continue

    flush_buffer()
    
    logging.info(f"Processamento concluído: {contador} arquivo(s) indexado(s)")
    return contador, resumo_pastas

if __name__ == "__main__":
    log_stream = configurar_logger()
    start = time.time()
    
    logging.info("INDEXADOR JURÍDICO - Iniciando execução")
    
    try:
        total, resumo_pastas = executar()
        tempo = (time.time() - start) / 60
        
        logging.info(f"Execução finalizada com sucesso")
        logging.info(f"Tempo total: {tempo:.2f} minutos")
        logging.info(f"Arquivos indexados: {total}")
        
        for pasta, qtd in resumo_pastas:
            logging.info(f"  {pasta}: {qtd} PDF(s)")
        
        resumo_html = "<ul>" + "".join([f"<li>{p}: {q} PDFs encontrados</li>" for p, q in resumo_pastas]) + "</ul>"
        logs_completos = log_stream.getvalue()
        
        enviar_notificacao(
            f"✅ Sucesso: {total} novos arquivos indexados",
            f"<h3>Relatório de Execução</h3>"
            f"<p><b>Tempo Total:</b> {tempo:.2f} min</p>"
            f"<p><b>Novos Documentos:</b> {total}</p>"
            f"<p><b>Pastas processadas:</b>{resumo_html}</p>"
            f"<hr><h4>Logs:</h4>"
            f"<pre style='background: #f4f4f4; padding: 10px; border: 1px solid #ddd; overflow-x: auto; white-space: pre-wrap;'>{logs_completos}</pre>"
        )
        logging.info("E-mail de sucesso enviado")
        
    except Exception as e:
        logging.error(f"ERRO CRÍTICO: {type(e).__name__}")
        logging.error(f"Mensagem: {str(e)}")
        logging.error(f"Traceback:\n{traceback.format_exc()}")
        
        resumo_html = "<ul>" + "".join([f"<li>{p}: {q} PDFs encontrados</li>" for p, q in resumo_pastas]) + "</ul>" if 'resumo_pastas' in locals() else "<p>Execução falhou antes da varredura de pastas.</p>"
        logs_completos = log_stream.getvalue()
        
        enviar_notificacao(
            "❌ Erro no Indexador",
            f"<h3>Falha Crítica Detectada</h3>"
            f"<pre style='color: red; background: #ffe6e6; padding: 10px; border: 1px solid #ff0000;'>{traceback.format_exc()}</pre>"
            f"<p><b>Pastas processadas (até a falha):</b>{resumo_html}</p>"
            f"<hr><h4>Logs:</h4>"
            f"<pre style='background: #f4f4f4; padding: 10px; border: 1px solid #ddd; overflow-x: auto; white-space: pre-wrap;'>{logs_completos}</pre>"
        )
        logging.error("E-mail de erro enviado")
        raise