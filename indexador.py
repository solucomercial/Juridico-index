import os
import hashlib
import uuid
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
OS_URL = f"https://{os.getenv('OS_HOST')}:{os.getenv('OS_PORT')}"
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
        resend.Emails.send({
            "from": f"Sistema Jurídico <{EMAIL_FROM}>",
            "to": EMAILS_TO,
            "cc": EMAILS_CC,
            "subject": assunto,
            "html": html
        })
    except Exception as e:
        logging.error(f"Erro no e-mail: {e}")

# ==================================================
# LOGGING
# ==================================================
def configurar_logger():
    """Cria logger que escreve em arquivo e também mantém cópia em memória."""
    log_stream = io.StringIO()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler("index.log")
    file_handler.setFormatter(formatter)

    memory_handler = logging.StreamHandler(log_stream)
    memory_handler.setFormatter(formatter)

    # Configura o logger raiz para capturar tudo
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(memory_handler)
    
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

def extrair_conteudo(caminho):
    texto_final = ""
    try:
        with pdfplumber.open(caminho) as pdf:
            for p in pdf.pages:
                t = p.extract_text()
                if t: texto_final += t + "\n"
        
        if len(texto_final.strip()) < 50:
            logging.info(f"Texto curto detectado em {caminho}. Iniciando OCR...")
            imagens = convert_from_path(caminho, dpi=200)
            for img in imagens:
                texto_final += pytesseract.image_to_string(img, lang="por") + "\n"
    except Exception as e:
        logging.warning(f"Erro na extração de {caminho}: {e}")
    return texto_final

# ==================================================
# LÓGICA PRINCIPAL
# ==================================================

def executar():
    m_client = MongoClient(MONGO_URI)
    colecao = m_client["juridico_db"]["arquivos"]
    
    os_client = OpenSearch(
        hosts=[OS_URL],
        http_auth=OS_AUTH,
        use_ssl=True, verify_certs=False, ssl_show_warn=False
    )

    arquivos = []
    resumo_pastas = []
    logging.info(f"Iniciando varredura em {len(PASTAS_DOCS)} pastas configuradas.")
    
    for pasta in PASTAS_DOCS:
        pasta_abs = os.path.abspath(pasta)
        if os.path.exists(pasta):
            logging.info(f"Buscando arquivos em: {pasta_abs}")
            novos_arquivos = [os.path.join(pasta, f) for f in os.listdir(pasta) if f.lower().endswith('.pdf')]
            logging.info(f"Encontrados {len(novos_arquivos)} PDFs em {pasta}")
            resumo_pastas.append((pasta_abs, len(novos_arquivos)))
            arquivos.extend(novos_arquivos)
        else:
            logging.warning(f"Caminho não encontrado ou inacessível: {pasta}")
            resumo_pastas.append((f"{pasta_abs} (inacessível)", 0))

    contador = 0
    buffer = []

    for caminho in tqdm(arquivos, desc="Processando"):
        caminho_completo = os.path.abspath(caminho)
        try:
            h = calcular_hash(caminho)
            
            # Verifica se já existe no MongoDB
            if colecao.find_one({"hash": h}):
                logging.info(f"Pulando (já indexado): {caminho_completo}")
                continue

            logging.info(f"Processando novo arquivo: {caminho_completo}")
            txt = extrair_conteudo(caminho)
            
            if not txt.strip():
                logging.warning(f"Conteúdo vazio após extração/OCR: {caminho_completo}")
                continue

            contador += 1
            buffer.append({
                "_index": OS_INDEX,
                "_id": str(uuid.uuid4()),
                "hash": h,
                "arquivo": os.path.basename(caminho),
                "conteudo": txt,
                "caminho_original": caminho_completo,
                "data": time.ctime()
            })

            if len(buffer) >= 100:
                helpers.bulk(os_client, buffer)
                colecao.insert_many([{"hash": d["hash"]} for d in buffer])
                logging.info(f"Lote de 100 arquivos enviado para OpenSearch/Mongo.")
                buffer = []
        except Exception as e:
            logging.error(f"Erro crítico ao processar {caminho_completo}: {e}")
            continue

    if buffer:
        helpers.bulk(os_client, buffer)
        colecao.insert_many([{"hash": d["hash"]} for d in buffer])
        logging.info(f"Lote final de {len(buffer)} arquivos enviado.")
    
    logging.info(f"Finalizado. Total de novos arquivos indexados: {contador}")
    return contador, resumo_pastas

if __name__ == "__main__":
    log_stream = configurar_logger()
    start = time.time()
    try:
        total, resumo_pastas = executar()
        tempo = (time.time() - start) / 60
        resumo_html = "<ul>" + "".join([f"<li>{p}: {q} PDFs encontrados</li>" for p, q in resumo_pastas]) + "</ul>"
        enviar_notificacao(
            f"✅ Sucesso: {total} novos arquivos",
            f"<h3>Relatório de Execução</h3>"
            f"<p><b>Tempo Total:</b> {tempo:.2f} min</p>"
            f"<p><b>Novos Documentos:</b> {total}</p>"
            f"<p><b>Pastas processadas:</b>{resumo_html}</p>"
            f"<hr><h4>Logs Detalhados:</h4>"
            f"<pre style='background: #f4f4f4; padding: 10px; border: 1px solid #ddd;'>{log_stream.getvalue()}</pre>"
        )
    except Exception:
        resumo_html = "<ul>" + "".join([f"<li>{p}: {q} PDFs encontrados</li>" for p, q in resumo_pastas]) + "</ul>" if 'resumo_pastas' in locals() else "<p>Execução falhou antes da varredura de pastas.</p>"
        enviar_notificacao(
            "❌ Erro no Indexador",
            f"<h3>Falha Crítica Detectada</h3>"
            f"<pre style='color: red;'>{traceback.format_exc()}</pre>"
            f"<p><b>Pastas processadas (até a falha):</b>{resumo_html}</p>"
            f"<hr><h4>Logs capturados antes da falha:</h4>"
            f"<pre style='background: #f4f4f4; padding: 10px; border: 1px solid #ddd;'>{log_stream.getvalue()}</pre>"
        )