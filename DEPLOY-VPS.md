# 🚀 Deploy do Baby Buddy na VPS — Checklist

Guia passo a passo para rodar o Baby Buddy **from-source** com **PostgreSQL + nginx + gunicorn + HTTPS**, assumindo **Ubuntu 24.04**.

> **Como usar:** abra este arquivo no PyCharm (ou qualquer editor com preview de Markdown) e vá marcando as caixinhas `[ ]` → `[x]` conforme for executando cada passo na VPS. Clicar na caixinha no preview do PyCharm/GitHub alterna o estado.

---

## 📝 Valores a preencher (anote antes de começar)

Substitua estes placeholders ao longo do guia:

| Placeholder | O que é | Seu valor                         |
|---|---|-----------------------------------|
| Domínio da VPS | Subdomínio que aponta pra VPS (já aplicado no guia) | ✅ `tedbaby.dmenos.com.br` |
| `SENHA_FORTE_DB` | Senha do usuário do Postgres (**use só letras/números** pra não quebrar a URL) | DEFINIR QUANDO CRIAR O BANCO      |
| `SECRET_KEY` | Chave gerada no passo 6 | CRIAR QUANDO CLONAR O REPOSITORIO |

- [x] Placeholders anotados

---

## ✅ Pré-requisitos

- [z] **DNS**: registro **A** de `tedbaby.dmenos.com.br` apontando pro **IP da VPS** (já propagado — teste com `ping tedbaby.dmenos.com.br`)
- [ ] ~~**Firewall**: portas 80 e 443 liberadas~~
  ```bash
  sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
  ```
- [ ] ~~Logado com um usuário **sudo** (não root direto)~~

---

## 1. Pacotes do sistema

- [x] Instalar pacotes base, Node 24 e pipenv
  ```bash
  sudo apt update
  sudo apt install -y python3 python3-venv python3-pip pipx git \
    postgresql nginx gettext \
    build-essential libpq-dev libjpeg-dev zlib1g-dev
  # Node 24 (pra buildar os assets)
  curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
  sudo apt install -y nodejs
  # pipenv — instale GLOBAL (/usr/local/bin) pra o usuário do app (babybuddy) também enxergar.
  # (um `pipx install pipenv` normal fica só no ~/.local do root e o babybuddy não acha.)
  sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install pipenv
  /usr/local/bin/pipenv --version   # confirma que ficou global
  ```

---

## 2. Banco PostgreSQL

- [x] Criar role e banco `babybuddy`
  ```bash
  sudo -u postgres psql -c "CREATE ROLE babybuddy WITH LOGIN PASSWORD 'SENHA_FORTE_DB' CREATEDB;"
  sudo -u postgres psql -c "CREATE DATABASE babybuddy OWNER babybuddy;"
  sudo -u postgres psql -d babybuddy -c "ALTER SCHEMA public OWNER TO babybuddy;"
  ```

---

## 3. Usuário do app + código

- [ ] Criar usuário de sistema e clonar o repositório
  ```bash
  sudo useradd --system --create-home --home-dir /opt/babybuddy --shell /bin/bash babybuddy
  sudo -u babybuddy git clone https://github.com/babybuddy/babybuddy.git /opt/babybuddy/app
  # (se for seu fork, troque a URL acima)
  ```

---

## 4. Dependências Python (venv dentro do projeto)

- [ ] Instalar deps com pipenv (`.venv` no diretório do app)
  ```bash
  sudo -u babybuddy -H bash -c '
    cd /opt/babybuddy/app
    export PIPENV_VENV_IN_PROJECT=1
    pipenv install
  '
  ```

---

## 5. Buildar os assets (Node/gulp)

- [ ] Instalar deps de frontend e compilar CSS/JS
  ```bash
  sudo -u babybuddy -H bash -c '
    cd /opt/babybuddy/app
    npm ci
    npx gulp build
  '
  ```

---

## 6. Arquivo `.env` de produção

- [ ] Gerar o `SECRET_KEY`
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(50))"
  ```
- [ ] Criar o `.env` (troque os placeholders e cole o SECRET_KEY gerado)
  ```bash
  sudo -u babybuddy tee /opt/babybuddy/app/.env >/dev/null <<'EOF'
  DJANGO_SETTINGS_MODULE=babybuddy.settings.production
  DEBUG=False
  SECRET_KEY=COLE_O_SECRET_KEY_AQUI
  ALLOWED_HOSTS=tedbaby.dmenos.com.br
  DATABASE_URL=postgres://babybuddy:SENHA_FORTE_DB@localhost:5432/babybuddy
  CSRF_TRUSTED_ORIGINS=https://tedbaby.dmenos.com.br
  SECURE_PROXY_SSL_HEADER=True
  SESSION_COOKIE_SECURE=True
  CSRF_COOKIE_SECURE=True
  EOF
  sudo chmod 600 /opt/babybuddy/app/.env
  ```
- [ ] Criar o settings de produção (fininho — toda config vem do `.env`)
  ```bash
  echo 'from .base import *' | sudo -u babybuddy tee /opt/babybuddy/app/babybuddy/settings/production.py >/dev/null
  ```

---

## 7. Migrar + estáticos + traduções

- [ ] Rodar migrations, compilar traduções e coletar estáticos
  ```bash
  sudo -u babybuddy -H bash -c '
    cd /opt/babybuddy/app
    pipenv run python manage.py migrate
    pipenv run python manage.py compilemessages
    pipenv run python manage.py collectstatic --noinput
    mkdir -p media
  '
  ```
  > O `migrate` cria o usuário **admin/admin** — a senha é trocada no passo 11.

---

## 8. Serviço systemd (gunicorn)

- [ ] Criar e habilitar o serviço
  ```bash
  sudo tee /etc/systemd/system/babybuddy.service >/dev/null <<'EOF'
  [Unit]
  Description=Baby Buddy (Gunicorn)
  After=network.target postgresql.service
  
  [Service]
  User=root
  Group=root
  WorkingDirectory=/srv/babybuddy/app
  EnvironmentFile=/srv/babybuddy/app/.env
  ExecStart=/srv/babybuddy/app/.venv/bin/gunicorn babybuddy.wsgi:application \
    --bind 127.0.0.1:8001 \
    --workers 3 \
    --timeout 60
  Restart=always
  
  [Install]
  WantedBy=multi-user.target
  EOF

  sudo systemctl daemon-reload
  sudo systemctl enable --now babybuddy
  ```
- [ ] Confirmar que está `active (running)`
  ```bash
  sudo systemctl status babybuddy --no-pager
  ```

---

## 9. nginx (proxy reverso + estáticos/mídia)

- [ ] Ajustar permissões pro nginx (www-data) ler os arquivos
  ```bash
  sudo chmod 755 /opt/babybuddy /opt/babybuddy/app
  sudo chmod -R o+rX /opt/babybuddy/app/static /opt/babybuddy/app/media
  ```
- [ ] Criar o site do nginx
  ```bash
  sudo tee /etc/nginx/sites-available/babybuddy >/dev/null <<'EOF'
  upstream babybuddy { server unix:/run/babybuddy/babybuddy.sock; }

  server {
      listen 80;
      server_name tedbaby.dmenos.com.br;
      client_max_body_size 10M;               # uploads de foto

      location /static/ { alias /opt/babybuddy/app/static/; expires 30d; }
      location /media/  { alias /opt/babybuddy/app/media/; }

      location / {
          proxy_pass http://babybuddy;
          proxy_set_header Host $host;
          proxy_set_header X-Real-IP $remote_addr;
          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
          proxy_set_header X-Forwarded-Proto $scheme;   # casa com SECURE_PROXY_SSL_HEADER
      }
  }
  EOF
  ```
- [ ] Ativar o site e recarregar
  ```bash
  sudo ln -sf /etc/nginx/sites-available/babybuddy /etc/nginx/sites-enabled/babybuddy
  sudo rm -f /etc/nginx/sites-enabled/default
  sudo nginx -t && sudo systemctl reload nginx
  ```
  > Neste ponto o site já responde em **http://tedbaby.dmenos.com.br** (sem HTTPS ainda).

---

## 10. HTTPS (Let's Encrypt)

- [ ] Emitir o certificado e configurar HTTPS + redirect
  ```bash
  sudo apt install -y certbot python3-certbot-nginx
  sudo certbot --nginx -d tedbaby.dmenos.com.br
  ```
  > O certbot edita o nginx pra 443 + redirect 80→443 e configura a **renovação automática**.

---

## 11. Finalizar

- [ ] Trocar a senha do admin
  ```bash
  sudo -u babybuddy -H bash -c 'cd /opt/babybuddy/app && pipenv run python manage.py changepassword admin'
  ```
- [ ] Acessar **https://tedbaby.dmenos.com.br**, logar com `admin` + nova senha ✅

---

## 🔄 Atualizar depois (deploy de nova versão)

```bash
sudo -u babybuddy -H bash -c '
  cd /opt/babybuddy/app
  git pull
  export PIPENV_VENV_IN_PROJECT=1
  pipenv install
  npm ci && npx gulp build
  pipenv run python manage.py migrate
  pipenv run python manage.py compilemessages
  pipenv run python manage.py collectstatic --noinput
'
sudo systemctl restart babybuddy
```

---

## ⚠️ Pontos de atenção

- **Backup do banco**: agende um `pg_dump` (ex.: no cron)
  ```bash
  sudo -u postgres pg_dump babybuddy | gzip > /opt/babybuddy/backup-$(date +%F).sql.gz
  ```
- **Segurança**: nunca deixe `DEBUG=True`. O `django-axes` já bloqueia brute-force de login (5 tentativas).
- **Logs**:
  ```bash
  sudo journalctl -u babybuddy -f     # app (gunicorn/django)
  sudo tail -f /var/log/nginx/error.log   # proxy
  ```
- **Node no servidor**: só é usado pra buildar. Pra economizar espaço pode `rm -rf node_modules` após o build (rode `npm ci` de novo só quando for atualizar).

---

## 🧭 Diagnóstico rápido (se algo não abrir)

| Sintoma | Onde olhar |
|---|---|
| 502 Bad Gateway | serviço caiu → `sudo systemctl status babybuddy` e `journalctl -u babybuddy -e` |
| CSS/JS quebrado | faltou `collectstatic` ou permissão em `static/` (passo 9) |
| CSRF / "Origin checking failed" | conferir `CSRF_TRUSTED_ORIGINS` e `SECURE_PROXY_SSL_HEADER` no `.env` |
| `DisallowedHost` | domínio faltando em `ALLOWED_HOSTS` no `.env` |
| Erro de conexão com o banco | conferir `DATABASE_URL` e se o Postgres está rodando (`systemctl status postgresql`) |
