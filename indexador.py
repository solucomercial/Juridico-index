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
# ALTERAÇÃO: Lendo a string e convertendo em uma lista de caminhos
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
# SISTEMA DE NOTIFICAÇÕES (Mantido igual)
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

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, memory_handler])
    return log_stream

# ==================================================
# FUNÇÕES TÉCNICAS (Mantido igual)
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
            imagens = convert_from_path(caminho, dpi=200)
            for img in imagens:
                texto_final += pytesseract.image_to_string(img, lang="por") + "\n"
    except Exception as e:
        logging.warning(f"Erro extração {caminho}: {e}")
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

    # ALTERAÇÃO: Coleta arquivos de todas as pastas configuradas
    arquivos = []
    for pasta in PASTAS_DOCS:
        if os.path.exists(pasta):
            logging.info(f"Buscando arquivos em: {pasta}")
            novos_arquivos = [os.path.join(pasta, f) for f in os.listdir(pasta) if f.lower().endswith('.pdf')]
            arquivos.extend(novos_arquivos)
        else:
            logging.warning(f"Caminho configurado não existe: {pasta}")

    contador = 0
    buffer = []

    for caminho in tqdm(arquivos, desc="Processando"):
        try:
            h = calcular_hash(caminho)
            if colecao.find_one({"hash": h}): continue

            txt = extrair_conteudo(caminho)
            if not txt.strip(): continue

            contador += 1
            buffer.append({
                "_index": OS_INDEX,
                "_id": str(uuid.uuid4()),
                "hash": h,
                "arquivo": os.path.basename(caminho),
                "conteudo": txt,
                "caminho_original": caminho, # Útil para saber de qual pasta veio
                "data": time.ctime()
            })

            if len(buffer) >= 100:
                helpers.bulk(os_client, buffer)
                colecao.insert_many([{"hash": d["hash"]} for d in buffer])
                buffer = []
        except Exception as e:
            logging.error(f"Erro ao processar {caminho}: {e}")
            continue

    if buffer:
        helpers.bulk(os_client, buffer)
        colecao.insert_many([{"hash": d["hash"]} for d in buffer])
    
    logging.info(f"Total processado: {contador}")
    return contador

if __name__ == "__main__":
    log_stream = configurar_logger()
    start = time.time()
    try:
        total = executar()
        tempo = (time.time() - start) / 60
        enviar_notificacao(
            f"✅ Sucesso: {total} novos arquivos",
            f"<p>Tempo: {tempo:.2f} min</p><pre>{log_stream.getvalue()}</pre>"
        )
    except Exception:
        enviar_notificacao(
            "❌ Erro no Indexador",
            f"<pre>{traceback.format_exc()}</pre><hr><pre>{log_stream.getvalue()}</pre>"
        )