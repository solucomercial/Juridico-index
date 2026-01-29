#!/bin/bash
# Script de Migração: indexador.py v1 → v2
# Este script facilita a transição de forma segura

set -e

echo "🚀 Iniciando Migração do Indexador Jurídico (v1 → v2)"
echo "=========================================="
echo ""

# Cores para output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 1. Verificar se estamos no diretório correto
if [ ! -f "indexador_v2.py" ]; then
    echo -e "${RED}❌ Erro: indexador_v2.py não encontrado no diretório atual${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Arquivo indexador_v2.py encontrado${NC}"
echo ""

# 2. Backup da versão atual
echo "📦 Criando backups..."
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

if [ -f "indexador.py" ]; then
    cp indexador.py "indexador_v1_backup_${TIMESTAMP}.py"
    echo -e "${GREEN}✓ Backup de indexador.py: indexador_v1_backup_${TIMESTAMP}.py${NC}"
fi

if [ -f ".env" ]; then
    cp .env ".env_backup_${TIMESTAMP}"
    echo -e "${GREEN}✓ Backup de .env: .env_backup_${TIMESTAMP}${NC}"
fi

if [ -f "docker-compose.yml" ]; then
    cp docker-compose.yml "docker-compose_v1_backup_${TIMESTAMP}.yml"
    echo -e "${GREEN}✓ Backup de docker-compose.yml: docker-compose_v1_backup_${TIMESTAMP}.yml${NC}"
fi

echo ""

# 3. Copiar nova versão
echo "🔄 Atualizando arquivos..."
cp indexador_v2.py indexador.py
echo -e "${GREEN}✓ indexador.py atualizado para v2${NC}"

if [ -f ".env.new" ]; then
    # Tentar mesclar .env existente com novo
    if [ -f ".env" ]; then
        echo -e "${YELLOW}⚠️  Arquivo .env existente. Revisar .env.new antes de usar.${NC}"
        echo "    Copiar .env.new para .env quando pronto."
    else
        cp .env.new .env
        echo -e "${GREEN}✓ .env criado a partir de .env.new${NC}"
    fi
fi

echo ""

# 4. Atualizar requirements.txt
echo "📚 Verificando dependências..."
if grep -q "tenacity" requirements.txt; then
    echo -e "${GREEN}✓ tenacity já está em requirements.txt${NC}"
else
    echo "tenacity" >> requirements.txt
    echo -e "${GREEN}✓ tenacity adicionado a requirements.txt${NC}"
fi

echo ""

# 5. Verificar configurações no .env
echo "⚙️  Verificando variáveis de ambiente..."
MISSING_VARS=0

check_env_var() {
    if ! grep -q "^$1=" .env 2>/dev/null; then
        echo -e "${YELLOW}⚠️  Variável não encontrada: $1${NC}"
        MISSING_VARS=$((MISSING_VARS + 1))
    fi
}

check_env_var "OS_PROTOCOL"
check_env_var "NFS_SERVER_JURIDICO"
check_env_var "NFS_SERVER_PEOPLE"
check_env_var "NFS_SERVER_SIGN"
check_env_var "NFS_SERVER_SIGN_ORIGINAL_FILES"
check_env_var "OCR_DPI"
check_env_var "MAX_WORKERS"

if [ $MISSING_VARS -eq 0 ]; then
    echo -e "${GREEN}✓ Todas as variáveis de ambiente estão configuradas${NC}"
else
    echo -e "${YELLOW}⚠️  $MISSING_VARS variáveis de ambiente faltam${NC}"
    echo "   Edite .env ou copie .env.new"
fi

echo ""

# 6. Verificar Dockerfile
echo "🐳 Verificando Dockerfile..."
if [ -f "dockerfile.multi-stage" ]; then
    echo -e "${GREEN}✓ dockerfile.multi-stage disponível${NC}"
    echo "   Para usar: docker build -f dockerfile.multi-stage -t indexador:v2 ."
else
    echo -e "${YELLOW}⚠️  dockerfile.multi-stage não encontrado${NC}"
fi

echo ""

# 7. Resumo
echo "=========================================="
echo -e "${GREEN}✅ Migração Concluída!${NC}"
echo "=========================================="
echo ""
echo "📋 Próximos passos:"
echo "   1. Revisar .env e adicionar variáveis faltantes (se houver)"
echo "   2. Instalar nova dependência: pip install tenacity"
echo "   3. Testar nova versão: python indexador.py"
echo "   4. Se necessário, reverter para backup: mv indexador_v1_backup_${TIMESTAMP}.py indexador.py"
echo ""
echo "📚 Documentação:"
echo "   - Leia MELHORIAS_v2.md para detalhes das mudanças"
echo "   - Leia README_RESUMO.txt para visão geral"
echo ""
echo "🔄 Backups criados:"
echo "   - indexador_v1_backup_${TIMESTAMP}.py"
echo "   - .env_backup_${TIMESTAMP}"
if [ -f "docker-compose_v1_backup_${TIMESTAMP}.yml" ]; then
    echo "   - docker-compose_v1_backup_${TIMESTAMP}.yml"
fi
echo ""
echo -e "${GREEN}Bom uso da v2!${NC} 🚀"
