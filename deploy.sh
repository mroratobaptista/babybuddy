#!/bin/bash
# Para o script se houver erro
# set -e

echo "=== Parando serviço ==="
sudo systemctl stop babybuddy.service

echo "=== Backup do .env ==="
cp ./app/.env .env

echo "=== Removendo app antiga ==="
rm -rf ./app

echo "=== Clonando repositório ==="
git clone https://github.com/mroratobaptista/babybuddy.git ./app

echo "=== Configurando ambiente ==="
cd app
cp ../.env .env

echo "=== Instalando dependências ==="
pipenv sync --python "$HOME/.pyenv/versions/$(cat .python-version)/bin/python"
npm ci
npx gulp build

echo "=== Rodando migrações ==="
pipenv run python manage.py migrate

echo "=== Compilando traduções ==="
pipenv run python manage.py compilemessages

echo "=== Coletando arquivos estáticos ==="
pipenv run python manage.py collectstatic --noinput

echo "=== Limpando node_modules (não é usado em runtime) ==="
rm -rf node_modules

echo "=== Voltando ao diretório anterior ==="
cd ..

echo "=== Iniciando serviço ==="
sudo systemctl start babybuddy.service
