#!/usr/bin/env bash
# Starts the tutor (and its JupyterLab) and opens the browser.
set -euo pipefail
cd "$(dirname "$0")"

[ -x .venv/bin/python ] || { echo "Run ./setup.sh first."; exit 1; }
# Settings saved by ./setup.sh (LLM choice, API keys). Variables already in your environment win.
if [ -f .env ]; then
  while IFS= read -r line; do
    [[ "$line" =~ ^([A-Z_]+)=(.*)$ ]] || continue
    name="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"; val="${val#\'}"; val="${val%\'}"
    [ -z "${!name:-}" ] && export "$name=$val"
  done < .env
fi

# LLM backend: TUTOR_LLM=llmgateway|anthropic|openai|claude-cli, or auto-detect when unset.
case "${TUTOR_LLM:-}" in
  llmgateway) [ -n "${LLMGATEWAY_API_KEY:-}" ] || { echo "LLMGATEWAY_API_KEY missing. Run ./setup.sh"; exit 1; };;
  anthropic) [ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "ANTHROPIC_API_KEY missing. Run ./setup.sh"; exit 1; };;
  openai)    [ -n "${OPENAI_API_KEY:-}" ] || { echo "OPENAI_API_KEY missing. Run ./setup.sh"; exit 1; };;
  openrouter) [ -n "${OPENROUTER_API_KEY:-}" ] || { echo "OPENROUTER_API_KEY missing. Run ./setup.sh"; exit 1; };;
  claude-cli) command -v claude >/dev/null || { echo "'claude' command not found. Run ./setup.sh"; exit 1; };;
  "")
    if [ -n "${LLMGATEWAY_API_KEY:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${OPENAI_API_KEY:-}" ] || [ -n "${OPENROUTER_API_KEY:-}" ] || command -v claude >/dev/null; then :
    else echo "No LLM configured. Run ./setup.sh"; exit 1; fi;;
esac
echo "LLM: ${TUTOR_LLM:-auto-detect}"

export TUTOR_PORT="${TUTOR_PORT:-8765}"
export TUTOR_JUPYTER_PORT="${TUTOR_JUPYTER_PORT:-8766}"

for p in "$TUTOR_PORT" "$TUTOR_JUPYTER_PORT"; do
  if lsof -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $p is already in use. Stop that process or set TUTOR_PORT / TUTOR_JUPYTER_PORT."
    exit 1
  fi
done

URL="http://localhost:$TUTOR_PORT"
# Open the browser once the server answers.
( for _ in $(seq 60); do
    if curl -fs "$URL/api/state" >/dev/null 2>&1; then
      { command -v open >/dev/null && open "$URL"; } || { command -v xdg-open >/dev/null && xdg-open "$URL"; } || true
      break
    fi
    sleep 1
  done ) &

exec .venv/bin/python app.py
