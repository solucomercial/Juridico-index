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


def normalizar_caminho(caminho):
    caminho_abs = os.path.abspath(caminho)
    if caminho_abs.startswith("/dados"):
        return caminho_abs.replace("/dados", "//10.130.1.99/DeptosMatriz/Juridico", 1)
    return caminho_abs

def extrair_conteudo(caminho):
    logging.info(f"Extraindo conteúdo de: {caminho}")
    paginas = []
    try:
        with pdfplumber.open(caminho) as pdf:
            logging.debug(f"PDF aberto com {len(pdf.pages)} página(s)")
            for i, p in enumerate(pdf.pages, 1):
                texto = (p.extract_text() or "").strip()

                if len(texto) < 50:
                    logging.debug(f"      Página {i}: texto curto ({len(texto)} chars), aplicando OCR...")
                    try:
                        imagens = convert_from_path(caminho, dpi=200, first_page=i, last_page=i)
                        if imagens:
                            ocr_texto = (pytesseract.image_to_string(imagens[0], lang="por") or "").strip()
                            logging.debug(f"      Página {i}: OCR concluído ({len(ocr_texto)} chars)")
                            texto = ocr_texto
                        else:
                            logging.debug(f"      Página {i}: OCR falhou (nenhuma imagem)")
                    except Exception as ocr_err:
                        logging.warning(f"      Página {i}: erro na OCR: {ocr_err}")
                else:
                    logging.debug(f"      Página {i}: texto extraído ({len(texto)} chars)")

                if texto:
                    paginas.append({"texto": texto, "pagina": i})
                else:
                    logging.debug(f"      Página {i}: conteúdo vazio (pulada)")
    except Exception as e:
        logging.warning(f"Erro na extração de {caminho}: {e}")
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

    logging.debug("Clientes criados com sucesso")

    arquivos = []
    resumo_pastas = []
    logging.info(f"Iniciando varredura RECURSIVA em {len(PASTAS_DOCS)} pastas configuradas.")
    
    for pasta in PASTAS_DOCS:
        pasta_abs = os.path.abspath(pasta)
        if os.path.exists(pasta):
            logging.info(f"\n📂 Processando pasta: {pasta_abs}")
            contador_pasta = 0
            
            # Percorre recursivamente todas as subpastas
            for root, dirs, files in os.walk(pasta_abs):
                pdfs_nesta_pasta = [os.path.join(root, f) for f in files if f.lower().endswith('.pdf')]
                if pdfs_nesta_pasta:
                    logging.info(f"   → {len(pdfs_nesta_pasta)} PDF(s) encontrado(s) em: {root}")
                    contador_pasta += len(pdfs_nesta_pasta)
                    arquivos.extend(pdfs_nesta_pasta)

                # Loga diretórios vazios ou sem PDFs em modo debug
                if not pdfs_nesta_pasta:
                    logging.debug(f"   → Nenhum PDF em: {root}")
            
            if contador_pasta == 0:
                logging.warning(f"   ⚠️  Nenhum PDF encontrado em {pasta_abs}")
            else:
                logging.info(f"   ✓ Total acumulado nesta pasta: {contador_pasta} PDF(s)")
            resumo_pastas.append((pasta_abs, contador_pasta))
        else:
            logging.warning(f"❌ Caminho não encontrado ou inacessível: {pasta}")
            resumo_pastas.append((f"{pasta_abs} (inacessível)", 0))

    contador = 0
    buffer = []
    novos_hashes = set()
    total_arquivos = len(arquivos)
    
    logging.info(f"\n{'='*70}")
    logging.info(f"📊 RESUMO DE DESCOBERTA")
    logging.info(f"{'='*70}")
    logging.info(f"Total de PDF(s) encontrado(s): {total_arquivos}")
    if total_arquivos == 0:
        logging.warning("⚠️  Nenhum arquivo PDF encontrado. Finalizando...")
        return 0, resumo_pastas
    logging.info(f"Iniciando processamento...")
    logging.info(f"{'='*70}\n")

    def flush_buffer():
        if buffer:
            helpers.bulk(os_client, buffer)
            logging.info(f"   ✅ Lote de {len(buffer)} páginas(s) enviado para OpenSearch")
            buffer.clear()
        if novos_hashes:
            colecao.insert_many([{"hash": h} for h in novos_hashes])
            logging.info(f"   ✅ {len(novos_hashes)} arquivo(s) registrado(s) no MongoDB")
            novos_hashes.clear()

    for idx, caminho in enumerate(arquivos, 1):
        caminho_completo = os.path.abspath(caminho)
        caminho_corrigido = normalizar_caminho(caminho_completo)
        arquivo_nome = os.path.basename(caminho_completo)
        
        logging.info(f"\n{'='*70}")
        logging.info(f"📄 [{idx}/{total_arquivos}] PROCESSANDO: {arquivo_nome}")
        logging.info(f"   Local: {caminho_completo}")
        logging.info(f"{'='*70}")
        
        try:
            h = calcular_hash(caminho)
            logging.info(f"   🔐 Hash: {h[:16]}...")
            
            # Verifica se já existe no MongoDB
            if colecao.find_one({"hash": h}):
                logging.info(f"   ⏭️  PULADO: Arquivo já foi indexado anteriormente")
                continue

            logging.info(f"   🔍 Extraindo conteúdo...")
            paginas = extrair_conteudo(caminho)

            if not paginas:
                logging.warning(f"   ⚠️  VAZIO: Nenhum conteúdo após extração/OCR")
                continue

            novos_hashes.add(h)
            contador += 1

            logging.info(f"   📖 {len(paginas)} página(s) extraída(s) com sucesso")
            
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
                logging.debug(f"      ├─ Página {pagina['pagina']}: {len(pagina['texto'])} caracteres")

                if len(buffer) >= 100:
                    logging.info(f"   💾 Buffer cheio ({len(buffer)} documentos), enviando lote...")
                    flush_buffer()
            
            logging.info(f"   ✓ Arquivo {arquivo_nome} adicionado ao buffer")
            
        except Exception as e:
            logging.error(f"   ❌ ERRO: {str(e)}")
            logging.debug(f"   Traceback: {traceback.format_exc()}")
            continue

    flush_buffer()
    
    logging.info(f"\n{'='*70}")
    logging.info(f"✅ PROCESSAMENTO CONCLUÍDO")
    logging.info(f"{'='*70}")
    logging.info(f"Total de arquivos processados: {contador}")
    logging.info(f"Total de páginas indexadas: {contador}")
    logging.info(f"{'='*70}\n")

if __name__ == "__main__":
    log_stream = configurar_logger()
    start = time.time()
    
    logging.info("\n")
    logging.info("╔" + "="*68 + "╗")
    logging.info("║" + "INDEXADOR JURÍDICO - SISTEMA DE INDEXAÇÃO".center(68) + "║")
    logging.info("╚" + "="*68 + "╝")
    logging.info("")
    
    try:
        total, resumo_pastas = executar()
        tempo = (time.time() - start) / 60
        
        logging.info("╔" + "="*68 + "╗")
        logging.info("║" + "✅ EXECUÇÃO FINALIZADA COM SUCESSO".center(68) + "║")
        logging.info("╚" + "="*68 + "╝")
        logging.info(f"⏱️  Tempo total: {tempo:.2f} minutos")
        logging.info(f"📁 Arquivos processados: {total}")
        logging.info("")
        
        for pasta, qtd in resumo_pastas:
            status = "✓" if qtd > 0 else "○"
            logging.info(f"   {status} {pasta}: {qtd} PDF(s)")
        
        logging.info("")
        
        resumo_html = "<ul>" + "".join([f"<li>{p}: {q} PDFs encontrados</li>" for p, q in resumo_pastas]) + "</ul>"
        logs_completos = log_stream.getvalue()
        
        enviar_notificacao(
            f"✅ Sucesso: {total} novos arquivos indexados",
            f"<h3>Relatório de Execução</h3>"
            f"<p><b>Tempo Total:</b> {tempo:.2f} min</p>"
            f"<p><b>Novos Documentos:</b> {total}</p>"
            f"<p><b>Pastas processadas:</b>{resumo_html}</p>"
            f"<hr><h4>Logs Detalhados (100% capturados):</h4>"
            f"<pre style='background: #f4f4f4; padding: 10px; border: 1px solid #ddd; overflow-x: auto; white-space: pre-wrap;'>{logs_completos}</pre>"
        )
        logging.info("📧 E-mail de sucesso enviado.")
        
    except Exception as e:
        logging.error("")
        logging.error("╔" + "="*68 + "╗")
        logging.error("║" + "❌ ERRO CRÍTICO NA EXECUÇÃO".center(68) + "║")
        logging.error("╚" + "="*68 + "╝")
        logging.error(f"🔴 Tipo de erro: {type(e).__name__}")
        logging.error(f"📝 Mensagem: {str(e)}")
        logging.error(f"📋 Traceback completo:\n{traceback.format_exc()}")
        logging.error("")
        
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
        logging.error("📧 E-mail de erro enviado.")
        raise