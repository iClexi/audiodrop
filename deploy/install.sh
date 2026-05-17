#!/usr/bin/env bash
# Install VideoDrop on an Ubuntu/Debian server.
# Usage: sudo bash deploy/install.sh
set -euo pipefail

APP_USER="${APP_USER:-infra}"
APP_DIR="${APP_DIR:-/opt/audiodrop}"
WORK_DIR="${WORK_DIR:-/tmp/audiodrop}"
PORT="${AUDIODROP_PORT:-3400}"
ADMIN_IP="${AUDIODROP_ADMIN_IP:-127.0.0.1}"
DATABASE_URL="${AUDIODROP_DATABASE_URL:-}"
RECAPTCHA_SITE_KEY="${AUDIODROP_RECAPTCHA_SITE_KEY:-}"
RECAPTCHA_SECRET_KEY="${AUDIODROP_RECAPTCHA_SECRET_KEY:-}"
RECAPTCHA_SCORE_THRESHOLD="${AUDIODROP_RECAPTCHA_SCORE_THRESHOLD:-0.45}"

if [[ $EUID -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

echo "==> Installing system dependencies"
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  ffmpeg ca-certificates curl

echo "==> Preparing user and directories"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/app" "$WORK_DIR" /etc/audiodrop
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$WORK_DIR"

echo "==> Copying code to $APP_DIR/app"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
rsync -a --delete --exclude '.venv' --exclude 'tmp' --exclude '.git' --exclude 'deploy' \
  "$SRC_DIR/" "$APP_DIR/app/"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/app"

echo "==> Creating venv in $APP_DIR/venv"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/app/requirements.txt"

echo "==> Writing /etc/audiodrop/audiodrop.env"
cat >/etc/audiodrop/audiodrop.env <<EOF
AUDIODROP_PORT=$PORT
AUDIODROP_WORK_DIR=$WORK_DIR
AUDIODROP_MAX_DURATION=1800
AUDIODROP_LOG_LEVEL=INFO
AUDIODROP_ADMIN_IP=$ADMIN_IP
AUDIODROP_DATABASE_URL=$DATABASE_URL
AUDIODROP_RECAPTCHA_SITE_KEY=$RECAPTCHA_SITE_KEY
AUDIODROP_RECAPTCHA_SECRET_KEY=$RECAPTCHA_SECRET_KEY
AUDIODROP_RECAPTCHA_SCORE_THRESHOLD=$RECAPTCHA_SCORE_THRESHOLD
EOF
chmod 640 /etc/audiodrop/audiodrop.env
chown root:"$APP_USER" /etc/audiodrop/audiodrop.env

echo "==> Installing systemd unit"
install -m 0644 "$SRC_DIR/deploy/audiodrop.service" /etc/systemd/system/audiodrop.service
systemctl daemon-reload
systemctl enable --now audiodrop.service

echo "==> VideoDrop started at http://127.0.0.1:$PORT"
echo "==> Install the Apache vhost if you want to expose it:"
echo "    sudo cp $SRC_DIR/deploy/006-audiodrop.conf /etc/apache2/sites-enabled/"
echo "    sudo systemctl reload apache2"
