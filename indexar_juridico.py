import os
import hashlib
import uuid
import time
import logging
import traceback
import resend
import urllib3
from dotenv import load_dotenv # Nova biblioteca
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
PASTA_DOCS = os.getenv("PASTA_DOCUMENTOS")
MONGO_URI = os.getenv("MONGO_URI")

# OpenSearch
OS_URL = f"https://{os.getenv('OS_HOST')}:{os.getenv('OS_PORT')}"
OS_AUTH = (os.getenv("OS_USER"), os.getenv("OS_PASS"))
OS_INDEX = os.getenv("OS_INDEX")

# Resend
resend.api_key = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_SENDER")
# Converter strings do .env em listas para a API
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

    arquivos = [os.path.join(PASTA_DOCS, f) for f in os.listdir(PASTA_DOCS) if f.lower().endswith('.pdf')]
    
    contador = 0
    buffer = []

    for caminho in tqdm(arquivos, desc="Processando"):
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
            "data": time.ctime()
        })

        if len(buffer) >= 100:
            helpers.bulk(os_client, buffer)
            colecao.insert_many([{"hash": d["hash"]} for d in buffer])
            buffer = []

    if buffer:
        helpers.bulk(os_client, buffer)
        colecao.insert_many([{"hash": d["hash"]} for d in buffer])
    
    return contador

if __name__ == "__main__":
    logging.basicConfig(filename='index.log', level=logging.INFO)
    start = time.time()
    try:
        total = executar()
        tempo = (time.time() - start) / 60
        enviar_notificacao(f"✅ Sucesso: {total} novos arquivos", f"<p>Tempo: {tempo:.2f} min</p>")
    except Exception:
        enviar_notificacao("❌ Erro no Indexador", f"<pre>{traceback.format_exc()}</pre>")