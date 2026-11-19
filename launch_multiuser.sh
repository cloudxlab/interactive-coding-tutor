#!/usr/bin/env bash
# Runs the multi-user tutor on this machine (Mac/Linux). For production use docker-compose.yml instead.
# Settings come from .env.multiuser (create it with: python3 setup_multiuser.py).
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env.multiuser ] || { echo "No .env.multiuser found. Run: python3 setup_multiuser.py"; exit 1; }
[ -x .venv/bin/python ] || { echo "Run ./setup.sh first (creates .venv)."; exit 1; }
.venv/bin/python -c "import authlib, itsdangerous, httpx, websockets, cryptography" 2>/dev/null || .venv/bin/pip install -q -r requirements.txt

set -a; . ./.env.multiuser; set +a   # values are single-quoted by the setup script
export TUTOR_MODE=multiuser

if [ "${TUTOR_ISOLATION:-docker}" = "docker" ] && ! docker image inspect "${TUTOR_DOCKER_IMAGE:-coding-tutor-jupyter}" >/dev/null 2>&1; then
  echo "Building the per-user Docker image (once)..."
  docker build -q -t "${TUTOR_DOCKER_IMAGE:-coding-tutor-jupyter}" -f docker/Dockerfile.jupyter .
fi

echo "Public URL: $TUTOR_BASE_URL   (listening on ${TUTOR_HOST:-127.0.0.1}:${TUTOR_PORT:-8765}; put an HTTPS reverse proxy in front for real use)"
exec .venv/bin/python app.py
