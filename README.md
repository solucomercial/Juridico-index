# 📂 Jurídico OCR & Indexer

Este projeto automatiza a leitura, processamento de OCR e indexação de documentos PDF em um servidor **Meilisearch**. Ele é capaz de identificar PDFs que já possuem texto (leitura direta) e PDFs que são apenas imagens (aplicando OCR via Tesseract).

## ✨ Funcionalidades

* **Extração Inteligente**: Detecta automaticamente se o PDF precisa de OCR ou se possui texto nativo.
* **OCR de Alta Resolução**: Utiliza Tesseract OCR com processamento de imagem via `pdf2image`.
* **Indexação Rápida**: Integração direta com Meilisearch para buscas de texto completo (Full-text search).
* **Monitoramento de Performance**: Relatórios de uso de CPU, memória e tempo de execução.
* **Suporte a WSL/Linux**: Otimizado para ambientes de alta performance.

## 🛠️ Tecnologias Utilizadas

* **Python 3.12+**
* **Meilisearch**: Motor de busca.
* **Tesseract OCR**: Mecanismo de reconhecimento óptico de caracteres.
* **pdfplumber**: Extração de texto de PDFs nativos.
* **psutil & tqdm**: Monitoramento e barras de progresso.

---

## 🚀 Como Configurar

### 1. Pré-requisitos Externos

Você precisará instalar as dependências do sistema para o OCR e processamento de imagem:

**Ubuntu/Debian/WSL:**
```bash
sudo apt update
sudo apt install tesseract-ocr tesseract-ocr-por poppler-utils -y

```

### 2. Instalação do Projeto

1. Clone o repositório:
```bash
git clone [https://github.com/seu-usuario/juridico-ocr.git](https://github.com/seu-usuario/juridico-ocr.git)
cd juridico-ocr

```


2. Crie e ative um ambiente virtual:
```bash
python3 -m venv venv
source venv/bin/activate

```


3. Instale as dependências:
```bash
pip install -r requirements.txt

```



### 3. Configuração das Variáveis de Ambiente

Crie um arquivo `.env` na raiz do projeto com base no modelo abaixo:

| Variável | Descrição | Exemplo |
| --- | --- | --- |
| `PASTA_DOCUMENTOS` | Caminho para a pasta contendo os PDFs. | `/caminho/para/pdf` |
| `MEILI_URL` | URL da instância do Meilisearch. | `http://localhost:7700` |
| `MEILI_API_KEY` | Chave mestra ou de escrita do Meilisearch. | `sua_chave_aqui` |
| `INDEX_NAME` | Nome do índice onde os dados serão salvos. | `juridico` |
| `IDIOMA_OCR` | Idioma utilizado pelo Tesseract. | `por` |

---

## 📈 Uso e Execução

Para iniciar o processo de indexação, execute:

```bash
python indexar_juridico.py

```

O script realizará o seguinte fluxo:

1. Varre a pasta configurada em busca de arquivos `.pdf`.
2. Verifica se o arquivo contém texto extraível.
3. Se não contiver, converte as páginas em imagens e aplica o OCR.
4. Envia os blocos de texto formatados para o Meilisearch.
5. Exibe um resumo técnico de performance ao final.

---

## 📝 Estrutura do Documento Indexado

Cada página de PDF é salva no Meilisearch com a seguinte estrutura JSON:

```json
{
  "id": "uuid-v4",
  "arquivo": "nome_do_arquivo.pdf",
  "pagina": 1,
  "caminho": "/caminho/completo/arquivo.pdf",
  "conteudo": "Texto extraído da página..."
}

```

---

Desenvolvido para fins de gestão de documentos jurídicos.
