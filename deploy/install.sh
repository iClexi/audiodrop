#!/usr/bin/env bash
# Instala AudioDrop en una VM Ubuntu/Debian.
# Uso: sudo bash deploy/install.sh
set -euo pipefail

APP_USER="${APP_USER:-infra}"
APP_DIR="${APP_DIR:-/opt/audiodrop}"
WORK_DIR="${WORK_DIR:-/tmp/audiodrop}"
PORT="${AUDIODROP_PORT:-3400}"
ADMIN_IP="${AUDIODROP_ADMIN_IP:-192.168.68.83}"
DATABASE_URL="${AUDIODROP_DATABASE_URL:-}"

if [[ $EUID -ne 0 ]]; then
  echo "Ejecuta este script con sudo." >&2
  exit 1
fi

echo "==> Instalando dependencias del sistema"
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  ffmpeg ca-certificates curl

echo "==> Preparando usuario y directorios"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/app" "$WORK_DIR" /etc/audiodrop
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$WORK_DIR"

echo "==> Copiando código a $APP_DIR/app"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
rsync -a --delete --exclude '.venv' --exclude 'tmp' --exclude '.git' --exclude 'deploy' \
  "$SRC_DIR/" "$APP_DIR/app/"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/app"

echo "==> Creando venv en $APP_DIR/venv"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/app/requirements.txt"

echo "==> Escribiendo /etc/audiodrop/audiodrop.env"
cat >/etc/audiodrop/audiodrop.env <<EOF
AUDIODROP_PORT=$PORT
AUDIODROP_WORK_DIR=$WORK_DIR
AUDIODROP_MAX_DURATION=1800
AUDIODROP_LOG_LEVEL=INFO
AUDIODROP_ADMIN_IP=$ADMIN_IP
AUDIODROP_DATABASE_URL=$DATABASE_URL
EOF
chmod 640 /etc/audiodrop/audiodrop.env
chown root:"$APP_USER" /etc/audiodrop/audiodrop.env

echo "==> Instalando unidad systemd"
install -m 0644 "$SRC_DIR/deploy/audiodrop.service" /etc/systemd/system/audiodrop.service
systemctl daemon-reload
systemctl enable --now audiodrop.service

echo "==> AudioDrop arrancado en http://127.0.0.1:$PORT"
echo "==> Pon el vhost de Apache si quieres exponerlo:"
echo "    sudo cp $SRC_DIR/deploy/006-audiodrop.conf /etc/apache2/sites-enabled/"
echo "    sudo systemctl reload apache2"
