#!/usr/bin/env bash
# Lanzador local. Útil para desarrollo en tu laptop o pruebas rápidas en la VM.
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

export PYTHONUNBUFFERED=1
export AUDIODROP_WORK_DIR="${AUDIODROP_WORK_DIR:-$(pwd)/tmp}"
mkdir -p "$AUDIODROP_WORK_DIR"

exec uvicorn main:app \
  --app-dir app \
  --host "${AUDIODROP_HOST:-0.0.0.0}" \
  --port "${AUDIODROP_PORT:-3400}" \
  --proxy-headers \
  --forwarded-allow-ips='*'
