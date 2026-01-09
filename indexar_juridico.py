import os
import uuid
import sys
import platform
import subprocess
import time
import psutil
from tqdm import tqdm

import meilisearch
import pytesseract
from pdf2image import convert_from_path
import pdfplumber


# ==================================================
# CONFIGURAÇÕES
# ==================================================
PASTA_DOCUMENTOS = os.getenv("PASTA_DOCUMENTOS", "./documentos")
MEILI_URL = os.getenv("MEILI_URL", "http://localhost:7700")
MEILI_API_KEY = os.getenv("MEILI_API_KEY")
INDEX_NAME = os.getenv("INDEX_NAME", "juridico")
IDIOMA_OCR = os.getenv("IDIOMA_OCR", "por")

# Validação básica
if not MEILI_API_KEY:
    print("❌ Erro: MEILI_API_KEY não configurada no arquivo .env")
    sys.exit(1)


# ==================================================
# CLIENTE MEILISEARCH
# ==================================================
client = meilisearch.Client(MEILI_URL, MEILI_API_KEY)
index = client.index(INDEX_NAME)


# ==================================================
# CONTADORES
# ==================================================
total_pdfs = 0
pdfs_lidos = 0
pdfs_ocr = 0
docs_indexados = 0
paginas_processadas = 0
task_uid_final = None


# ==================================================
# PERFORMANCE
# ==================================================
process = psutil.Process(os.getpid())
cpu_samples = []
start_time = time.perf_counter()


# ==================================================
# FUNÇÕES AUXILIARES
# ==================================================
def versao_tesseract():
    try:
        out = subprocess.check_output(["tesseract", "--version"], text=True)
        return out.split("\n")[0]
    except Exception:
        return "Não identificado"


def pdf_tem_texto(caminho_pdf):
    try:
        with pdfplumber.open(caminho_pdf) as pdf:
            for page in pdf.pages:
                texto = page.extract_text()
                if texto and texto.strip():
                    return True
    except Exception:
        pass
    return False


def extrair_texto_pdf(caminho_pdf):
    documentos = []
    global paginas_processadas

    with pdfplumber.open(caminho_pdf) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            texto = page.extract_text()
            paginas_processadas += 1
            cpu_samples.append(psutil.cpu_percent(interval=None))

            if texto and texto.strip():
                documentos.append({
                    "id": str(uuid.uuid4()),
                    "arquivo": os.path.basename(caminho_pdf),
                    "pagina": i,
                    "caminho": caminho_pdf,
                    "conteudo": texto
                })

    return documentos


def ocr_pdf(caminho_pdf):
    documentos = []
    global paginas_processadas

    imagens = convert_from_path(caminho_pdf, dpi=300)

    for i, imagem in enumerate(imagens, start=1):
        texto = pytesseract.image_to_string(imagem, lang=IDIOMA_OCR)
        paginas_processadas += 1
        cpu_samples.append(psutil.cpu_percent(interval=None))

        if texto.strip():
            documentos.append({
                "id": str(uuid.uuid4()),
                "arquivo": os.path.basename(caminho_pdf),
                "pagina": i,
                "caminho": caminho_pdf,
                "conteudo": texto
            })

    return documentos


# ==================================================
# PROCESSAMENTO PRINCIPAL
# ==================================================
def processar_pasta():
    global total_pdfs, pdfs_lidos, pdfs_ocr, docs_indexados, task_uid_final

    documentos_para_indexar = []

    pdfs = []
    for root, _, files in os.walk(PASTA_DOCUMENTOS):
        for file in files:
            if file.lower().endswith(".pdf"):
                pdfs.append(os.path.join(root, file))

    total_pdfs = len(pdfs)

    for caminho in tqdm(pdfs, desc="Processando PDFs"):
        cpu_samples.append(psutil.cpu_percent(interval=None))

        if pdf_tem_texto(caminho):
            docs = extrair_texto_pdf(caminho)
            pdfs_lidos += 1
        else:
            docs = ocr_pdf(caminho)
            pdfs_ocr += 1

        documentos_para_indexar.extend(docs)

    docs_indexados = len(documentos_para_indexar)

    if documentos_para_indexar:
        task = index.add_documents(documentos_para_indexar)
        task_uid_final = task.task_uid

        print("\n📤 Documentos enviados ao Meilisearch")
        print(f"🆔 Task UID: {task_uid_final}")
        print("⏳ Indexação em andamento no servidor (modo assíncrono)")


# ==================================================
# RESUMOS
# ==================================================
def resumo_final():
    print("\n📊 RESUMO DA INDEXAÇÃO")
    print("=" * 45)
    print(f"📂 Origem dos documentos: {PASTA_DOCUMENTOS}")
    print(f"📂 PDFs processados: {total_pdfs}")
    print(f"📄 PDFs lidos direto: {pdfs_lidos}")
    print(f"📷 PDFs com OCR: {pdfs_ocr}")
    print(f"📑 Páginas processadas: {paginas_processadas}")
    print(f"🧾 Documentos enviados: {docs_indexados}")
    print(f"🆔 Task Meilisearch: {task_uid_final}")
    print("=" * 45)
    print("✅ INDEXAÇÃO DISPARADA COM SUCESSO")


def resumo_tecnico():
    end_time = time.perf_counter()
    tempo_execucao = end_time - start_time
    cpu_media = sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0
    memoria_mb = process.memory_info().rss / (1024 ** 2)

    print("\n🛠️ RESUMO TÉCNICO & PERFORMANCE")
    print("=" * 45)

    print(f"🖥️ Sistema Operacional: {platform.system()} {platform.release()}")
    print(f"🐧 Ambiente WSL: {'Sim' if 'microsoft' in platform.release().lower() else 'Não'}")
    print(f"🐍 Python: {sys.version.split()[0]}")
    print(f"📦 Virtualenv: {os.environ.get('VIRTUAL_ENV', 'Não identificado')}")

    print("\n⚙️ Recursos do Sistema:")
    print(f"• Núcleos CPU: {psutil.cpu_count(logical=True)}")
    print(f"• Uso médio de CPU: {cpu_media:.2f}%")
    print(f"• Uso de memória RAM: {memoria_mb:.2f} MB")
    print(f"• Tempo total de execução: {tempo_execucao:.2f} segundos")

    print("\n📚 Tecnologias Utilizadas:")
    print("• OCR: Tesseract OCR")
    print("• PDF Parser: pdfplumber")
    print("• OCR Engine: pytesseract")
    print("• Busca: Meilisearch")

    print("\n🔍 OCR:")
    print(f"• Idioma: {IDIOMA_OCR}")
    print(f"• Versão Tesseract: {versao_tesseract()}")

    print("\n🚀 Indexação:")
    print(f"• Índice: {INDEX_NAME}")
    print(f"• Task UID: {task_uid_final}")

    print("=" * 45)


# ==================================================
# MAIN
# ==================================================
if __name__ == "__main__":
    processar_pasta()
    resumo_final()
    resumo_tecnico()
