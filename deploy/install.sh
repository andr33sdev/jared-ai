#!/usr/bin/env bash
# Instala Jared en un VPS con Ubuntu 22.04/24.04 o Debian 12.
# Uso (como root):  curl -fsSL https://raw.githubusercontent.com/andr33sdev/jared-ai/main/deploy/install.sh | bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/andr33sdev/jared-ai.git}"
APP_DIR=/opt/jared
ENV_FILE="$APP_DIR/.env"

if [ "$(id -u)" -ne 0 ]; then echo "Ejecutalo como root (sudo -i)"; exit 1; fi

echo "==> Instalando paquetes"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl gnupg debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null 2>&1; then
  echo "==> Instalando Caddy (HTTPS automático)"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

echo "==> Descargando Jared"
id jared >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin jared
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

echo "==> Dirección pública"
IP="$(curl -4 -fsS https://api.ipify.org)"
DOMAIN="${DOMAIN:-$(echo "$IP" | tr . -).sslip.io}"
echo "    $DOMAIN"

if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/.env.example" "$ENV_FILE"
fi
if grep -q '^PUBLIC_URL=' "$ENV_FILE"; then
  sed -i "s#^PUBLIC_URL=.*#PUBLIC_URL=https://$DOMAIN#" "$ENV_FILE"
else
  echo "PUBLIC_URL=https://$DOMAIN" >> "$ENV_FILE"
fi
chown -R jared:jared "$APP_DIR"
chmod 600 "$ENV_FILE"

echo "==> Servicio"
cat > /etc/systemd/system/jared.service <<UNIT
[Unit]
Description=Jared, asistente personal
After=network-online.target
Wants=network-online.target

[Service]
User=jared
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/uvicorn jared.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    reverse_proxy 127.0.0.1:8000
}
CADDY

cat > /usr/local/bin/jared-update <<'UPD'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/jared
sudo -u jared git pull --ff-only
sudo -u jared /opt/jared/.venv/bin/pip install -r requirements.txt -q
systemctl restart jared
echo "Jared actualizado."
UPD
chmod +x /usr/local/bin/jared-update

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q active; then
  ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp
fi

systemctl daemon-reload
systemctl enable jared caddy >/dev/null
systemctl restart caddy

echo
echo "============================================================"
echo " Falta un paso: cargar tus claves."
echo "   nano $ENV_FILE"
echo " Copiá los valores que tenés en Render (Environment) y guardá."
echo " Después:  systemctl restart jared"
echo " Para ver si anda:  journalctl -u jared -f"
echo " Dirección de Jared: https://$DOMAIN"
echo "============================================================"
