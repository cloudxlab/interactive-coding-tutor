#!/usr/bin/env bash
# Interactive setup: creates .venv, installs dependencies, and asks which LLM backend to use.
# Writes the choice (and API key, if any) to .env, which launch.sh reads.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "python3 not found"; exit 1; }
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "Python 3.9+ required"; exit 1; }

echo "=== Python Coding Tutor setup ==="
echo
if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  "$PY" -m venv .venv
fi
echo "Installing dependencies (JupyterLab is large; this can take a few minutes)..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m ipykernel install --sys-prefix --name python3 >/dev/null
mkdir -p workspace
echo "Dependencies installed."
echo

# ---- LLM choice -------------------------------------------------------------
if [ ! -t 0 ]; then
  echo "Not an interactive terminal; skipping LLM selection. Re-run ./setup.sh in a terminal to choose."
  exit 0
fi

# Remember previous answers so re-running setup can keep them.
OLD_LLM=""; OLD_LLMGATEWAY=""; OLD_ANTHROPIC=""; OLD_OPENAI=""
if [ -f .env ]; then
  set -a; . ./.env; set +a
  OLD_LLM="${TUTOR_LLM:-}"; OLD_ANTHROPIC="${ANTHROPIC_API_KEY:-}"; OLD_OPENAI="${OPENAI_API_KEY:-}"; OLD_LLMGATEWAY="${LLMGATEWAY_API_KEY:-}"
  [ -n "$OLD_LLM" ] && echo "Current LLM setting: $OLD_LLM"
fi

open_url() {
  if command -v open >/dev/null; then open "$1" 2>/dev/null || true
  elif command -v xdg-open >/dev/null; then xdg-open "$1" 2>/dev/null || true; fi
}

guide_anthropic() {
  cat >&2 <<'G'

--- How to get an Anthropic API key ---
  1. Go to https://console.anthropic.com and sign in (or create an account).
  2. Add credits under Settings > Billing (the API is pay-as-you-go; the tutor
     uses a few small requests per problem).
  3. Open Settings > API Keys and click "Create Key". Name it e.g. "coding-tutor".
  4. Copy the key right away (it starts with "sk-ant-"); it is only shown once.
G
  read -r -p "Open the console in your browser now? [Y/n] " o >&2
  case "$o" in [nN]*) ;; *) open_url "https://console.anthropic.com/settings/keys";; esac
  echo >&2
}

guide_openai() {
  cat >&2 <<'G'

--- How to get an OpenAI API key ---
  1. Go to https://platform.openai.com and sign in (or create an account).
     Note: a ChatGPT subscription does not include API access.
  2. Add a payment method / credits under Settings > Billing.
  3. Open https://platform.openai.com/api-keys and click "Create new secret key".
  4. Copy the key right away (it starts with "sk-"); it is only shown once.
G
  read -r -p "Open the API keys page in your browser now? [Y/n] " o >&2
  case "$o" in [nN]*) ;; *) open_url "https://platform.openai.com/api-keys";; esac
  echo >&2
}

guide_claude_cli() {
  cat >&2 <<'G'

--- How to set up the claude command ---
  1. Install Claude Code:  npm install -g @anthropic-ai/claude-code
     (or see https://docs.claude.com/en/docs/claude-code/setup for other installers).
  2. Run `claude` once in a terminal and sign in when prompted (Claude Pro/Max
     subscription or an Anthropic Console account).
  3. Confirm it works:  claude -p "say hi"
  Then re-run ./setup.sh and choose this option again.
G
}

guide_llmgateway() {
  cat >&2 <<'G'

--- How to get an LLM Gateway API key ---
LLM Gateway is one account that gives you access to many AI models through a single API.
  1. Go to https://llmgateway.io and sign up (email, GitHub or Google), or log in.
  2. Add credit or a payment method in the dashboard if your plan needs it.
  3. Create a project, open its "API Keys" page and click "Create API Key".
  4. Copy the key right away (it usually starts with "llmgtwy_"); it may only be shown once.
G
  read -r -p "Open llmgateway.io in your browser now? [Y/n] " o >&2
  case "$o" in [nN]*) ;; *) open_url "https://llmgateway.io";; esac
  echo >&2
}

verify_llmgateway() {  # $1 = key. Sets MODELS_LIST. Returns 1 only if LLM Gateway rejects the key.
  local rc=0
  MODELS_LIST="$(LLMGATEWAY_API_KEY="$1" .venv/bin/python llmgateway_models.py 2>/dev/null)" || rc=$?
  case "$rc" in
    0) echo "Key verified. Some models you can use:"; echo "$MODELS_LIST" | head -12 | sed 's/^/    /';;
    2) echo "LLM Gateway rejected that key. Please check it and try again."; return 1;;
    *) echo "Couldn't reach LLM Gateway to verify the key (network or proxy?). Continuing without verifying."; MODELS_LIST="";;
  esac
}

ask_key() {  # prompts for a key; $3 = expected prefix  # $1=label  $2=existing value -> echoes key
  local key=""
  if [ -n "$2" ]; then
    read -r -p "$1 key already saved. Keep it? [Y/n] " keep >&2
    case "$keep" in [nN]*) ;; *) echo "$2"; return;; esac
  fi
  while [ -z "$key" ]; do
    read -r -s -p "Paste your $1 API key (input hidden): " key >&2; echo >&2
    key="$(echo "$key" | tr -d '[:space:]')"
    if [ -n "$key" ] && [[ "$key" != $3* ]]; then
      echo "That doesn't look like an $1 key (expected it to start with \"$3\")." >&2
      read -r -p "Use it anyway? [y/N] " yn >&2
      case "$yn" in [yY]*) ;; *) key="";; esac
    fi
  done
  echo "$key"
}

LLM=""; LLMGATEWAY_KEY=""; ANTHROPIC_KEY=""; OPENAI_KEY=""; MODELS_LIST=""
while [ -z "$LLM" ]; do
  echo "Which LLM should the tutor use?"
  echo "  1) LLM Gateway  (API key; many models through one API)  <- default"
  echo "  2) Anthropic    (API key)"
  echo "  3) OpenAI       (API key)"
  echo "  4) claude command line (uses your Claude Code login, no key needed)"
  read -r -p "Choose [1-4, press Enter for 1]: " choice
  case "${choice:-1}" in
    1) LLM=llmgateway
       if [ -z "$OLD_LLMGATEWAY" ]; then guide_llmgateway; fi
       while true; do
         LLMGATEWAY_KEY="$(ask_key "LLM Gateway" "$OLD_LLMGATEWAY" llmgtwy_)"
         if verify_llmgateway "$LLMGATEWAY_KEY"; then break; fi
         OLD_LLMGATEWAY=""   # don't offer to keep a rejected key
       done;;
    2) LLM=anthropic
       if [ -z "$OLD_ANTHROPIC" ]; then guide_anthropic; fi
       ANTHROPIC_KEY="$(ask_key Anthropic "$OLD_ANTHROPIC" sk-ant-)";;
    3) LLM=openai
       if [ -z "$OLD_OPENAI" ]; then guide_openai; fi
       OPENAI_KEY="$(ask_key OpenAI "$OLD_OPENAI" sk-)"
       echo "Installing the openai package..."; .venv/bin/pip install -q openai;;
    4) if command -v claude >/dev/null; then LLM=claude-cli
       else echo "The 'claude' command was not found on your PATH."; guide_claude_cli
       read -r -p "Press Enter to go back to the menu..." _; echo; fi;;
    *) echo "Please enter 1, 2, 3 or 4."; echo;;
  esac
done

if [ "$LLM" = "llmgateway" ]; then
  while true; do
    read -r -p "Model (press Enter for gpt-4o): " MODEL
    pick="${MODEL:-gpt-4o}"
    if [ -z "$MODELS_LIST" ] || echo "$MODELS_LIST" | grep -qxF "$pick"; then break; fi
    echo "'$pick' is not in your LLM Gateway model list above. Pick one of those (or press Ctrl+C to stop)."
  done
else
  read -r -p "Model override (press Enter for the default): " MODEL
fi

umask 077
{
  echo "TUTOR_LLM=$LLM"
  [ -n "$LLMGATEWAY_KEY" ] && echo "LLMGATEWAY_API_KEY='$LLMGATEWAY_KEY'"
  [ -n "$ANTHROPIC_KEY" ] && echo "ANTHROPIC_API_KEY='$ANTHROPIC_KEY'"
  [ -n "$OPENAI_KEY" ] && echo "OPENAI_API_KEY='$OPENAI_KEY'"
  [ -n "$MODEL" ] && echo "TUTOR_MODEL='$MODEL'"
  true
} > .env
chmod 600 .env

echo
echo "Saved to .env (readable only by you). LLM: $LLM"
echo "Setup complete. Start the tutor with: ./launch.sh"
