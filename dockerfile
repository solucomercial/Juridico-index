FROM python:3.12-slim

# Dependências do sistema para OCR e PDF
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-por \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ajustado para o novo nome do ficheiro
CMD ["python", "indexador.py"]