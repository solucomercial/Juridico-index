# 📂 Jurídico OCR & Indexer

Este projeto automatiza a leitura, processamento de OCR e indexação de documentos PDF em um servidor **OpenSearch**. Ele é capaz de identificar PDFs que já possuem texto (leitura direta) e PDFs que são apenas imagens (aplicando OCR via Tesseract). Utiliza MongoDB para rastrear arquivos processados e Resend para notificações por e-mail.

## ✨ Funcionalidades

* **Extração Inteligente**: Detecta automaticamente se o PDF precisa de OCR ou se possui texto nativo.
* **OCR de Alta Resolução**: Utiliza Tesseract OCR com processamento de imagem via `pdf2image`.
* **Indexação Rápida**: Integração direta com OpenSearch para buscas de texto completo (Full-text search).
* **Rastreamento de Arquivos**: Usa MongoDB para evitar reprocessamento de arquivos já indexados.
* **Notificações por E-mail**: Envia relatórios de sucesso ou erro via Resend.
* **Interface Web**: Página HTML simples para realizar buscas nos documentos indexados.
* **Suporte a WSL/Linux/Windows**: Otimizado para diversos ambientes.

## 🛠️ Tecnologias Utilizadas

* **Python 3.12+**
* **OpenSearch**: Motor de busca.
* **MongoDB**: Banco de dados para rastreamento.
* **Resend**: Serviço de e-mail para notificações.
* **Tesseract OCR**: Mecanismo de reconhecimento óptico de caracteres.
* **pdfplumber**: Extração de texto de PDFs nativos.
* **psutil & tqdm**: Monitoramento e barras de progresso (usados implicitamente).

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
| `MONGO_URI` | URI de conexão com MongoDB. | `mongodb://localhost:27017` |
| `OS_HOST` | Host do OpenSearch. | `localhost` |
| `OS_PORT` | Porta do OpenSearch. | `9200` |
| `OS_USER` | Usuário do OpenSearch. | `admin` |
| `OS_PASS` | Senha do OpenSearch. | `password` |
| `OS_INDEX` | Nome do índice no OpenSearch. | `juridico` |
| `RESEND_API_KEY` | Chave da API do Resend. | `sua_chave_aqui` |
| `EMAIL_SENDER` | E-mail do remetente. | `noreply@exemplo.com` |
| `EMAIL_TO` | Lista de e-mails destinatários (separados por vírgula). | `user1@exemplo.com,user2@exemplo.com` |
| `EMAIL_CC` | Lista de e-mails em cópia (separados por vírgula). | `cc@exemplo.com` |
| `IDIOMA_OCR` | Idioma utilizado pelo Tesseract. | `por` |

---

## 📈 Uso e Execução

### Indexação de Documentos

Para iniciar o processo de indexação, execute:

**Linux/WSL:**
```bash
python indexar_juridico.py
```

**Windows:**
```batch
executar_indexador.bat
```

O script realizará o seguinte fluxo:

1. Varre a pasta configurada em busca de arquivos `.pdf`.
2. Verifica se o arquivo já foi processado (usando hash armazenado no MongoDB).
3. Extrai o texto diretamente ou aplica OCR se necessário.
4. Indexa o conteúdo no OpenSearch.
5. Envia notificação por e-mail com o resultado.

### Interface Web de Busca

Abra o arquivo `index.html` em um navegador web para realizar buscas nos documentos indexados. A interface permite pesquisar termos no conteúdo dos PDFs e visualizar trechos destacados.

Certifique-se de que o OpenSearch esteja acessível e as credenciais estejam corretas no código do HTML (atualmente hardcoded).

---

## 📝 Estrutura do Documento Indexado

Cada PDF é salvo no OpenSearch com a seguinte estrutura JSON:

```json
{
  "id": "uuid-v4",
  "hash": "sha256-hash-do-arquivo",
  "arquivo": "nome_do_arquivo.pdf",
  "conteudo": "Texto extraído do PDF...",
  "data": "Data de indexação"
}

```

---

Desenvolvido para fins de gestão de documentos jurídicos.
