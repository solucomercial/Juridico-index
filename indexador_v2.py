import os
import hashlib
import time
import logging
import traceback
import resend
import urllib3
import io
import gc
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import partial
from dotenv import load_dotenv 
from pymongo import MongoClient
from opensearchpy import OpenSearch, helpers
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError
from tqdm import tqdm
import pytesseract
from pdf2image import convert_from_path
import pdfplumber
from tenacity import retry, stop_after_attempt, wait_exponential

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
OS_PROTOCOL = os.getenv("OS_PROTOCOL", "http")
OS_URL = f"{OS_PROTOCOL}://{os.getenv('OS_HOST')}:{os.getenv('OS_PORT')}"
OS_AUTH = (os.getenv("OS_USER"), os.getenv("OS_PASS"))
OS_INDEX = os.getenv("OS_INDEX")

# NFS Servers (movido para .env - suporta múltiplas pastas com IPs diferentes)
NFS_SERVER_JURIDICO = os.getenv("NFS_SERVER_JURIDICO", "//10.130.1.99/DeptosMatriz/Juridico")
NFS_SERVER_PEOPLE = os.getenv("NFS_SERVER_PEOPLE", "//172.17.0.10/h$/People")
NFS_SERVER_SIGN = os.getenv("NFS_SERVER_SIGN", "//172.17.0.10/h$/sign")
NFS_SERVER_SIGN_ORIGINAL_FILES = os.getenv("NFS_SERVER_SIGN_ORIGINAL_FILES", "//172.17.0.10/h$/sign_original_files")

# Dicionário de mapeamento de caminhos locais para servidores NFS
NFS_SERVERS = {
    "/juridico": NFS_SERVER_JURIDICO,
    "/people": NFS_SERVER_PEOPLE,
    "/sign": NFS_SERVER_SIGN,
    "/sign_original_files": NFS_SERVER_SIGN_ORIGINAL_FILES
}

# OCR Configuration - DPI aumentado para 300 (melhora precisão)
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))

# Resend
resend.api_key = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_SENDER")
EMAILS_TO = os.getenv("EMAIL_TO").split(",")
EMAILS_CC = os.getenv("EMAIL_CC").split(",")

# ==================================================
# SISTEMA DE NOTIFICAÇÕES
# ==================================================

def enviar_notificacao(assunto, html, caminho_log_arquivo=None):
    """
    Envia notificação por e-mail com resumo e logs em anexo.
    
    Melhorias:
    - Apenas resumo no corpo da mensagem (não logs completos)
    - Logs anexados como arquivo .txt se disponível
    - Evita sobrecarga de RAM e limites de tamanho de e-mail
    
    Args:
        assunto: Assunto do e-mail
        html: Conteúdo HTML do e-mail (resumido)
        caminho_log_arquivo: Caminho para arquivo de log para anexar (opcional)
    """
    try:
        logging.info(f"Enviando e-mail com assunto: {assunto}")
        
        email_data = {
            "from": f"Sistema Jurídico <{EMAIL_FROM}>",
            "to": EMAILS_TO,
            "cc": EMAILS_CC,
            "subject": assunto,
            "html": html
        }
        
        # Se houver um arquivo de log, anexar como arquivo
        if caminho_log_arquivo and os.path.exists(caminho_log_arquivo):
            try:
                with open(caminho_log_arquivo, "r", encoding="utf-8") as f:
                    log_conteudo = f.read()
                
                # Resend espera conteúdo base64
                import base64
                encoded = base64.b64encode(log_conteudo.encode("utf-8")).decode()
                
                email_data["attachments"] = [
                    {
                        "filename": "logs.txt",
                        "content": encoded
                    }
                ]
                logging.info("Log anexado ao e-mail")
            except Exception as attach_err:
                logging.warning(f"Não foi possível anexar log: {attach_err}")
        
        resend.Emails.send(email_data)
        logging.info("E-mail enviado com sucesso.")
    except Exception as e:
        logging.error(f"Erro no e-mail: {e}")

# ==================================================
# LOGGING
# ==================================================
def configurar_logger():
    """Cria logger que escreve em arquivo e também em memória para relatórios."""
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
    """Calcula hash SHA256 do arquivo para detecção de duplicatas."""
    sha = hashlib.sha256()
    with open(caminho, "rb") as f:
        while chunk := f.read(8192):
            sha.update(chunk)
    return sha.hexdigest()


def normalizar_caminho(caminho):
    """
    Normaliza o caminho substituindo caminhos locais pelos servidores NFS configurados.
    
    Melhoria: Suporta múltiplas pastas, cada uma com seu respectivo servidor NFS
    Todas as configurações vêm do .env em vez de hardcoded
    
    Mapeamento:
    - /juridico -> NFS_SERVER_JURIDICO
    - /people -> NFS_SERVER_PEOPLE
    - /sign -> NFS_SERVER_SIGN
    - /sign_original_files -> NFS_SERVER_SIGN_ORIGINAL_FILES
    """
    caminho_abs = os.path.abspath(caminho)
    
    # Tenta normalizar usando cada servidor NFS
    for caminho_local, nfs_server in NFS_SERVERS.items():
        if caminho_abs.startswith(caminho_local):
            return caminho_abs.replace(caminho_local, nfs_server, 1)
    
    return caminho_abs

def extrair_conteudo(caminho):
    """
    Extrai conteúdo de um PDF usando pdfplumber e OCR se necessário.
    
    Melhorias:
    - DPI aumentado para 300 (melhor precisão em documentos de baixa qualidade)
    - Context manager para garantir liberação de memória
    - Garbage collection explícito após OCR
    - Melhor logging de erros específicos
    
    Args:
        caminho: Caminho para o arquivo PDF
        
    Returns:
        Lista de dicionários com texto e número de página
    """
    paginas = []
    try:
        with pdfplumber.open(caminho) as pdf:
            for i, p in enumerate(pdf.pages, 1):
                texto = (p.extract_text() or "").strip()

                # Se texto extraído é muito pequeno, tenta OCR
                if len(texto) < 50:
                    try:
                        # Usa context manager para garantir liberação de memória
                        imagens = convert_from_path(caminho, dpi=OCR_DPI, first_page=i, last_page=i)
                        if imagens:
                            try:
                                ocr_texto = (pytesseract.image_to_string(imagens[0], lang="por") or "").strip()
                                texto = ocr_texto
                            finally:
                                # Libera memória da imagem explicitamente
                                del imagens[0]
                                del imagens
                        
                        # Force garbage collection após OCR para evitar vazamento de memória
                        gc.collect()
                    except Exception as ocr_err:
                        logging.warning(f"Erro OCR na página {i} de {os.path.basename(caminho)}: {ocr_err}")

                if texto:
                    paginas.append({"texto": texto, "pagina": i})
    except Exception as e:
        # Registra erro específico de abertura do PDF
        logging.error(f"Erro ao abrir/processar {os.path.basename(caminho)}: {type(e).__name__}: {str(e)}")
    
    return paginas

# ==================================================
# LÓGICA PRINCIPAL
# ==================================================

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10))
def criar_cliente_opensearch():
    """
    Cria cliente OpenSearch com retry automático em caso de falha.
    
    Melhorias:
    - Implementa retry com backoff exponencial (até 5 tentativas)
    - Aguarda 2-10 segundos entre tentativas
    - Timeout de 30 segundos
    - Lida com instabilidades momentâneas da rede
    """
    try:
        client = OpenSearch(
            hosts=[OS_URL],
            http_auth=OS_AUTH,
            use_ssl=(OS_PROTOCOL == "https"),
            verify_certs=False,
            ssl_show_warn=False,
            timeout=30,
            max_retries=3
        )
        # Testa a conexão
        client.info()
        logging.info("Conexão com OpenSearch estabelecida com sucesso")
        return client
    except OpenSearchConnectionError as e:
        logging.error(f"Falha ao conectar ao OpenSearch: {e}. Tentando novamente...")
        raise


def processar_arquivo(caminho, colecao):
    """
    Função de worker para processamento paralelo.
    
    Melhorias:
    - Processa arquivos em paralelo usando ProcessPoolExecutor
    - Cada worker executa independentemente
    - Reduz drasticamente o tempo de processamento para muitos PDFs
    
    Args:
        caminho: Caminho do PDF
        colecao: Coleção MongoDB
        
    Returns:
        Tupla (sucesso, hash, arquivo_nome, paginas, caminho_corrigido, erro)
    """
    try:
        caminho_completo = os.path.abspath(caminho)
        arquivo_nome = os.path.basename(caminho_completo)
        caminho_corrigido = normalizar_caminho(caminho_completo)
        
        # Calcula hash
        h = calcular_hash(caminho)
        
        # Verifica se já existe
        if colecao.find_one({"hash": h}):
            return (False, h, arquivo_nome, [], caminho_corrigido, "Já indexado")
        
        # Extrai conteúdo
        paginas = extrair_conteudo(caminho)
        
        if not paginas:
            return (False, h, arquivo_nome, [], caminho_corrigido, "Sem conteúdo")
        
        return (True, h, arquivo_nome, paginas, caminho_corrigido, None)
    
    except Exception as e:
        logging.error(f"Erro ao processar {caminho}: {type(e).__name__}: {str(e)}")
        return (False, None, os.path.basename(caminho), [], None, str(e))


def executar():
    """Função principal de execução do indexador."""
    logging.info("Iniciando conexão com MongoDB e OpenSearch")
    m_client = MongoClient(MONGO_URI)
    colecao = m_client["juridico_ocr"]["arquivos"]
    
    os_client = criar_cliente_opensearch()

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
    erros = []
    total_arquivos = len(arquivos)
    
    logging.info(f"Total de PDF(s) encontrado(s): {total_arquivos}")
    if total_arquivos == 0:
        logging.warning("Nenhum arquivo PDF encontrado. Finalizando...")
        return 0, resumo_pastas, erros
    
    logging.info(f"Iniciando processamento paralelo com {MAX_WORKERS} workers...")

    def flush_buffer():
        if buffer:
            helpers.bulk(os_client, buffer)
            buffer.clear()
        if novos_hashes:
            colecao.insert_many([{"hash": h} for h in novos_hashes])
            novos_hashes.clear()

    # Processamento paralelo com ProcessPoolExecutor
    # Melhoria: Distribui processamento entre múltiplos núcleos
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(processar_arquivo, caminho, colecao): caminho 
                   for caminho in arquivos}
        
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

                            if len(buffer) >= 100:
                                flush_buffer()
                    elif erro:
                        erros.append(f"{arquivo_nome}: {erro}")
                        
                except Exception as e:
                    erros.append(f"Erro ao processar future: {str(e)}")
                
                pbar.update(1)

    flush_buffer()
    
    logging.info(f"Processamento concluído: {contador} arquivo(s) indexado(s)")
    if erros:
        logging.warning(f"Total de erros/avisos: {len(erros)}")
    
    return contador, resumo_pastas, erros

if __name__ == "__main__":
    log_stream = configurar_logger()
    start = time.time()
    
    logging.info("INDEXADOR JURÍDICO - Iniciando execução (v2 - COM MELHORIAS)")
    
    try:
        total, resumo_pastas, erros = executar()
        tempo = (time.time() - start) / 60
        
        logging.info(f"Execução finalizada com sucesso")
        logging.info(f"Tempo total: {tempo:.2f} minutos")
        logging.info(f"Arquivos indexados: {total}")
        
        for pasta, qtd in resumo_pastas:
            logging.info(f"  {pasta}: {qtd} PDF(s)")
        
        # Preparar resumo HTML com informações resumidas (NOT logs)
        # Melhoria: Apenas resumo no corpo, logs em anexo
        resumo_html = "<ul>" + "".join([f"<li>{p}: {q} PDFs</li>" for p, q in resumo_pastas]) + "</ul>"
        
        if erros:
            erros_resumo = f"<p><b>Erros/Avisos ({len(erros)}):</b></p><ul>" + \
                           "".join([f"<li>{e}</li>" for e in erros[:10]]) + \
                           ("".join([f"</ul><p>... e {len(erros) - 10} mais</p>"]) if len(erros) > 10 else "</ul>")
        else:
            erros_resumo = ""
        
        enviar_notificacao(
            f"✅ Sucesso: {total} novos arquivos indexados",
            f"<h3>Relatório de Execução</h3>"
            f"<p><b>Tempo Total:</b> {tempo:.2f} min</p>"
            f"<p><b>Novos Documentos:</b> {total}</p>"
            f"<p><b>Pastas processadas:</b>{resumo_html}</p>"
            f"{erros_resumo}"
            f"<p><i>Os logs detalhados foram salvos em anexo (index.log)</i></p>",
            caminho_log_arquivo="index.log"
        )
        logging.info("E-mail de sucesso enviado")
        
    except Exception as e:
        logging.error(f"ERRO CRÍTICO: {type(e).__name__}")
        logging.error(f"Mensagem: {str(e)}")
        logging.error(f"Traceback:\n{traceback.format_exc()}")
        
        resumo_html = "<ul>" + "".join([f"<li>{p}: {q} PDFs</li>" for p, q in (resumo_pastas if 'resumo_pastas' in locals() else [])]) + "</ul>" \
                      if 'resumo_pastas' in locals() else "<p>Execução falhou antes da varredura de pastas.</p>"
        
        enviar_notificacao(
            "❌ Erro no Indexador",
            f"<h3>Falha Crítica Detectada</h3>"
            f"<p><b>Tipo de Erro:</b> {type(e).__name__}</p>"
            f"<p><b>Mensagem:</b> {str(e)}</p>"
            f"<p><b>Pastas processadas (até a falha):</b>{resumo_html}</p>"
            f"<p><i>Os logs detalhados foram salvos em anexo (index.log)</i></p>",
            caminho_log_arquivo="index.log"
        )
        logging.error("E-mail de erro enviado")
        raise
