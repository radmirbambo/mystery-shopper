#!/usr/bin/env bash
# Разворачивает бэкенд mystery-shopper на чистом Ubuntu 22/24 одной командой.
#   scp mystery-shopper.zip root@IP:/root/
#   ssh root@IP 'bash -s ms.твой-домен.ru' < deploy.sh
# или, уже на сервере: bash deploy.sh ms.твой-домен.ru
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then echo "Использование: bash deploy.sh ms.твой-домен.ru"; exit 1; fi

echo "== 1/5 пакеты"
apt-get update -qq
apt-get install -y -qq python3-pip python3-venv unzip curl debian-keyring debian-archive-keyring apt-transport-https >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

echo "== 2/5 код"
mkdir -p /opt/ms && cd /opt/ms
ZIP=$(ls /root/mystery-shopper*.zip /opt/ms/mystery-shopper*.zip 2>/dev/null | head -1 || true)
if [ -n "$ZIP" ]; then unzip -oq "$ZIP" -d /opt/ms; fi
[ -d /opt/ms/mystery-shopper/server ] || { echo "Не нашёл mystery-shopper.zip в /root — скопируй его туда"; exit 1; }
cd /opt/ms/mystery-shopper/server
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "== 3/5 токен"
if [ -f /etc/ms.env ]; then
  TOKEN=$(grep MS_TOKEN /etc/ms.env | cut -d= -f2)
else
  TOKEN=$(openssl rand -hex 16)
  echo "MS_TOKEN=$TOKEN" > /etc/ms.env
  chmod 600 /etc/ms.env
fi

echo "== 4/5 сервис"
cat > /etc/systemd/system/ms.service << EOF
[Unit]
Description=mystery-shopper backend
After=network.target
[Service]
WorkingDirectory=/opt/ms/mystery-shopper/server
EnvironmentFile=/etc/ms.env
ExecStart=/opt/ms/mystery-shopper/server/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now ms
systemctl restart ms

echo "== 5/5 https"
cat > /etc/caddy/Caddyfile << EOF
$DOMAIN {
    reverse_proxy 127.0.0.1:8000
}
EOF
systemctl enable --now caddy
systemctl restart caddy
sleep 3

echo
echo "================================================================"
echo "Готово."
echo "BACKEND: https://$DOMAIN/"
echo "TOKEN:   $TOKEN        (лежит в /etc/ms.env)"
echo
echo "Проверка (с ноутбука):"
echo "  curl -H 'X-Token: $TOKEN' https://$DOMAIN/report"
echo "Эти же два значения вставить в voximplant/scenario.js (BACKEND, TOKEN)"
echo "и в переменные окружения бота (MS_BACKEND, MS_TOKEN)."
echo "================================================================"
