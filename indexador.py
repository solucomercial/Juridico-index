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
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler("index.log", encoding='utf-8')
    file_handler.setFormatter(formatter)

    memory_handler = logging.StreamHandler(log_stream)
    memory_handler.setFormatter(formatter)

    # Configura o logger raiz para capturar tudo
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

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
    logging.debug(f"Calculando hash para: {caminho}")
    sha = hashlib.sha256()
    with open(caminho, "rb") as f:
        while chunk := f.read(8192):
            sha.update(chunk)
    digest = sha.hexdigest()
    logging.debug(f"Hash calculado: {digest}")
    return digest

def extrair_conteudo(caminho):
    logging.info(f"Extraindo conteúdo de: {caminho}")
    texto_final = ""
    try:
        with pdfplumber.open(caminho) as pdf:
            logging.debug(f"PDF aberto com {len(pdf.pages)} página(s)")
            for i, p in enumerate(pdf.pages, 1):
                t = p.extract_text()
                if t:
                    logging.debug(f"Página {i}: texto extraído ({len(t)} chars)")
                    texto_final += t + "\n"
                else:
                    logging.debug(f"Página {i}: sem texto detectado pelo parser")
        
        if len(texto_final.strip()) < 50:
            logging.info(f"Texto curto detectado em {caminho}. Iniciando OCR...")
            imagens = convert_from_path(caminho, dpi=200)
            logging.debug(f"OCR: {len(imagens)} página(s) convertidas para imagem")
            for i, img in enumerate(imagens, 1):
                ocr_texto = pytesseract.image_to_string(img, lang="por")
                logging.debug(f"OCR página {i}: {len(ocr_texto)} chars extraídos")
                texto_final += ocr_texto + "\n"
    except Exception as e:
        logging.warning(f"Erro na extração de {caminho}: {e}")
    return texto_final

# ==================================================
# LÓGICA PRINCIPAL
# ==================================================

def executar():
    logging.info("Iniciando conexão com MongoDB e OpenSearch")
    m_client = MongoClient(MONGO_URI)
    colecao = m_client["juridico_db"]["arquivos"]
    
    os_client = OpenSearch(
        hosts=[OS_URL],
        http_auth=OS_AUTH,
        use_ssl=(OS_PROTOCOL == "https"), # Só ativa SSL se o protocolo for https
        verify_certs=False, 
        ssl_show_warn=False
    )

    logging.debug("Clientes criados com sucesso")

    arquivos = []
    resumo_pastas = []
    logging.info(f"Iniciando varredura RECURSIVA em {len(PASTAS_DOCS)} pastas configuradas.")
    
    for pasta in PASTAS_DOCS:
        pasta_abs = os.path.abspath(pasta)
        if os.path.exists(pasta):
            logging.info(f"Buscando arquivos recursivamente em: {pasta_abs}")
            contador_pasta = 0
            
            # Percorre recursivamente todas as subpastas
            for root, dirs, files in os.walk(pasta_abs):
                pdfs_nesta_pasta = [os.path.join(root, f) for f in files if f.lower().endswith('.pdf')]
                if pdfs_nesta_pasta:
                    logging.info(f"  → {len(pdfs_nesta_pasta)} PDFs encontrados em: {root}")
                    contador_pasta += len(pdfs_nesta_pasta)
                    arquivos.extend(pdfs_nesta_pasta)

                # Loga diretórios vazios ou sem PDFs em modo debug
                if not pdfs_nesta_pasta:
                    logging.debug(f"  → Nenhum PDF em: {root}")
            
            logging.info(f"Total acumulado em {pasta_abs}: {contador_pasta} PDFs")
            resumo_pastas.append((pasta_abs, contador_pasta))
        else:
            logging.warning(f"Caminho não encontrado ou inacessível: {pasta}")
            resumo_pastas.append((f"{pasta_abs} (inacessível)", 0))

    contador = 0
    buffer = []
    total_arquivos = len(arquivos)
    logging.info(f"Total de {total_arquivos} arquivos para processar.")

    for idx, caminho in enumerate(arquivos, 1):
        caminho_completo = os.path.abspath(caminho)
        logging.info(f"[{idx}/{total_arquivos}] Verificando: {caminho_completo}")
        try:
            h = calcular_hash(caminho)
            
            # Verifica se já existe no MongoDB
            if colecao.find_one({"hash": h}):
                logging.debug(f"Pulando (já indexado): {os.path.basename(caminho_completo)}")
                continue

            logging.info(f"✓ Processando novo arquivo: {os.path.basename(caminho_completo)}")
            txt = extrair_conteudo(caminho)
            
            if not txt.strip():
                logging.warning(f"Conteúdo vazio após extração/OCR: {caminho_completo}")
                continue

            contador += 1
            logging.debug(f"Adicionando arquivo ao buffer: {os.path.basename(caminho_completo)}")
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
    logging.info("="*50)
    logging.info("INICIANDO INDEXADOR JURÍDICO")
    logging.info("="*50)
    
    try:
        total, resumo_pastas = executar()
        tempo = (time.time() - start) / 60
        logging.info("="*50)
        logging.info(f"EXECUÇÃO CONCLUÍDA COM SUCESSO")
        logging.info(f"Tempo total: {tempo:.2f} minutos")
        logging.info(f"Novos arquivos indexados: {total}")
        logging.info("="*50)
        
        resumo_html = "<ul>" + "".join([f"<li>{p}: {q} PDFs encontrados</li>" for p, q in resumo_pastas]) + "</ul>"
        logs_completos = log_stream.getvalue()
        
        enviar_notificacao(
            f"✅ Sucesso: {total} novos arquivos",
            f"<h3>Relatório de Execução</h3>"
            f"<p><b>Tempo Total:</b> {tempo:.2f} min</p>"
            f"<p><b>Novos Documentos:</b> {total}</p>"
            f"<p><b>Pastas processadas:</b>{resumo_html}</p>"
            f"<hr><h4>Logs Detalhados (100% capturados):</h4>"
            f"<pre style='background: #f4f4f4; padding: 10px; border: 1px solid #ddd; overflow-x: auto; white-space: pre-wrap;'>{logs_completos}</pre>"
        )
        logging.info("E-mail de sucesso enviado.")
        
    except Exception as e:
        logging.error("="*50)
        logging.error("ERRO CRÍTICO NA EXECUÇÃO")
        logging.error("="*50)
        logging.error(f"Tipo de erro: {type(e).__name__}")
        logging.error(f"Mensagem: {str(e)}")
        logging.error(f"Traceback completo:\n{traceback.format_exc()}")
        
        resumo_html = "<ul>" + "".join([f"<li>{p}: {q} PDFs encontrados</li>" for p, q in resumo_pastas]) + "</ul>" if 'resumo_pastas' in locals() else "<p>Execução falhou antes da varredura de pastas.</p>"
        logs_completos = log_stream.getvalue()
        
        enviar_notificacao(
            "❌ Erro no Indexador",
            f"<h3>Falha Crítica Detectada</h3>"
            f"<pre style='color: red; background: #ffe6e6; padding: 10px; border: 1px solid #ff0000;'>{traceback.format_exc()}</pre>"
            f"<p><b>Pastas processadas (até a falha):</b>{resumo_html}</p>"
            f"<hr><h4>Logs Completos (100% capturados antes da falha):</h4>"
            f"<pre style='background: #f4f4f4; padding: 10px; border: 1px solid #ddd; overflow-x: auto; white-space: pre-wrap;'>{logs_completos}</pre>"
        )
        logging.error("E-mail de erro enviado.")
        raise