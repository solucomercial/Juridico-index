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
# CONFIGURAÇÕES
# ==================================================
PASTAS_DOCS_RAW = os.getenv("PASTA_DOCUMENTOS", "")
# O split por ";" permite que você cole todos os caminhos no .env
PASTAS_DOCS = [p.strip() for p in PASTAS_DOCS_RAW.split(";") if p.strip()]

MONGO_URI = os.getenv("MONGO_URI")

# OpenSearch
OS_PROTOCOL = os.getenv("OS_PROTOCOL", "http")
OS_URL = f"{OS_PROTOCOL}://{os.getenv('OS_HOST')}:{os.getenv('OS_PORT')}"
OS_AUTH = (os.getenv("OS_USER"), os.getenv("OS_PASS"))
OS_INDEX = os.getenv("OS_INDEX")

# Resend
resend.api_key = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_SENDER")
EMAILS_TO = os.getenv("EMAIL_TO").split(",")
EMAILS_CC = os.getenv("EMAIL_CC", "").split(",") if os.getenv("EMAIL_CC") else []

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
        logging.error(f"Erro ao enviar e-mail: {e}")

# ==================================================
# LOGGING
# ==================================================
def configurar_logger():
    log_stream = io.StringIO()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler("index.log", encoding='utf-8')
    file_handler.setFormatter(formatter)

    memory_handler = logging.StreamHandler(log_stream)
    memory_handler.setFormatter(formatter)

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
        # Tenta extração direta de texto (PDFs "nativos")
        with pdfplumber.open(caminho) as pdf:
            for p in pdf.pages:
                t = p.extract_text()
                if t: texto_final += t + "\n"
        
        # Se extraiu pouco texto, tenta OCR (PDFs digitalizados)
        if len(texto_final.strip()) < 50:
            logging.info(f"Iniciando OCR: {os.path.basename(caminho)}")
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
        use_ssl=(OS_PROTOCOL == "https"),
        verify_certs=False, 
        ssl_show_warn=False
    )

    arquivos_para_processar = []
    resumo_pastas = []
    
    logging.info(f"Iniciando varredura em {len(PASTAS_DOCS)} diretórios raiz.")
    
    for pasta_raiz in PASTAS_DOCS:
        if os.path.exists(pasta_raiz):
            logging.info(f"Varrendo recursivamente: {pasta_raiz}")
            count_pasta = 0
            # os.walk permite entrar em todas as subpastas automaticamente
            for raiz, dirs, files in os.walk(pasta_raiz):
                for f in files:
                    if f.lower().endswith('.pdf'):
                        arquivos_para_processar.append(os.path.join(raiz, f))
                        count_pasta += 1
            
            resumo_pastas.append((pasta_raiz, count_pasta))
            logging.info(f"Concluído: {count_pasta} PDFs encontrados em {pasta_raiz}")
        else:
            logging.warning(f"Caminho inacessível: {pasta_raiz}")
            resumo_pastas.append((f"{pasta_raiz} (Inacessível)", 0))

    contador_novos = 0
    buffer = []

    for caminho in tqdm(arquivos_para_processar, desc="Indexando"):
        try:
            # Pegar hash para evitar duplicidade
            h = calcular_hash(caminho)
            if colecao.find_one({"hash": h}):
                continue

            txt = extrair_conteudo(caminho)
            if not txt.strip():
                continue

            contador_novos += 1
            buffer.append({
                "_index": OS_INDEX,
                "_id": str(uuid.uuid4()),
                "hash": h,
                "arquivo": os.path.basename(caminho),
                "conteudo": txt,
                "caminho_original": caminho,
                "data_indexacao": time.strftime('%Y-%m-%d %H:%M:%S')
            })

            # Envio em lotes (Bulk) para performance
            if len(buffer) >= 50: # Reduzi para 50 para evitar timeouts em arquivos pesados
                helpers.bulk(os_client, buffer)
                colecao.insert_many([{"hash": d["hash"]} for d in buffer])
                buffer = []
                
        except Exception as e:
            logging.error(f"Erro ao processar {caminho}: {e}")
            continue

    if buffer:
        helpers.bulk(os_client, buffer)
        colecao.insert_many([{"hash": d["hash"]} for d in buffer])
    
    return contador_novos, resumo_pastas

if __name__ == "__main__":
    log_stream = configurar_logger()
    start_time = time.time()
    try:
        total, resumo = executar()
        duracao = (time.time() - start_time) / 60
        
        lista_html = "".join([f"<li><code>{p}</code>: <b>{q}</b> arquivos</li>" for p, q in resumo])
        
        enviar_notificacao(
            f"✅ Indexação Concluída: {total} novos arquivos",
            f"<h2>Relatório de Indexação</h2>"
            f"<p><b>Duração:</b> {duracao:.2f} minutos</p>"
            f"<p><b>Novos itens adicionados:</b> {total}</p>"
            f"<h3>Resumo por Origem:</h3><ul>{lista_html}</ul>"
            f"<hr><h4>Logs:</h4><pre style='font-size:11px;'>{log_stream.getvalue()}</pre>"
        )
    except Exception:
        error_msg = traceback.format_exc()
        logging.error(error_msg)
        enviar_notificacao(
            "❌ Falha no Script de Indexação",
            f"<h3>Erro Crítico</h3><pre style='color:red;'>{error_msg}</pre>"
            f"<h4>Logs antes da falha:</h4><pre>{log_stream.getvalue()}</pre>"
        )