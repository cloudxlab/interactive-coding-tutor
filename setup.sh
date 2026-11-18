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
OLD_LLM=""; OLD_ANTHROPIC=""; OLD_OPENAI=""; OLD_OPENROUTER=""
if [ -f .env ]; then
  set -a; . ./.env; set +a
  OLD_LLM="${TUTOR_LLM:-}"; OLD_ANTHROPIC="${ANTHROPIC_API_KEY:-}"; OLD_OPENAI="${OPENAI_API_KEY:-}"; OLD_OPENROUTER="${OPENROUTER_API_KEY:-}"
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

guide_openrouter() {
  cat >&2 <<'G'

--- How to get an OpenRouter API key ---
OpenRouter is one account that gives you access to many AI models (Claude, GPT, Gemini, ...).
  1. Go to https://openrouter.ai and sign in (or create an account).
  2. Add credits at https://openrouter.ai/settings/credits (pay-as-you-go; some models are free).
  3. EITHER let this script sign you in through your browser (easiest), OR create a key yourself at
     https://openrouter.ai/settings/keys ("Create Key") and copy it (it starts with "sk-or-").
G
  echo >&2
}

get_openrouter_key() {  # $1 = existing key; prints the key to stdout
  if [ -n "$1" ]; then
    read -r -p "OpenRouter key already saved. Keep it? [Y/n] " keep >&2
    case "$keep" in [nN]*) ;; *) echo "$1"; return;; esac
  fi
  echo "How do you want to give the tutor your OpenRouter key?" >&2
  echo "  1) Sign in with my browser (OAuth, recommended: no copy/paste)" >&2
  echo "  2) I'll paste a key" >&2
  read -r -p "Choose [1-2]: " how >&2
  if [ "$how" != "2" ]; then
    local k
    if k="$(.venv/bin/python openrouter_oauth.py --print-key)" && [ -n "$k" ]; then echo "$k"; return; fi
    echo "Browser sign-in did not complete. Let's paste a key instead." >&2
  fi
  ask_key OpenRouter "" sk-or-
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

LLM=""; ANTHROPIC_KEY=""; OPENAI_KEY=""; OPENROUTER_KEY=""
while [ -z "$LLM" ]; do
  echo "Which LLM should the tutor use?"
  echo "  1) Anthropic  (API key)"
  echo "  2) OpenAI     (API key)"
  echo "  3) OpenRouter (API key or browser sign-in; many models)"
  echo "  4) claude command line (uses your Claude Code login, no key needed)"
  read -r -p "Choose [1-4]: " choice
  case "$choice" in
    1) LLM=anthropic
       if [ -z "$OLD_ANTHROPIC" ]; then guide_anthropic; fi
       ANTHROPIC_KEY="$(ask_key Anthropic "$OLD_ANTHROPIC" sk-ant-)";;
    2) LLM=openai
       if [ -z "$OLD_OPENAI" ]; then guide_openai; fi
       OPENAI_KEY="$(ask_key OpenAI "$OLD_OPENAI" sk-)"
       echo "Installing the openai package..."; .venv/bin/pip install -q openai;;
    3) LLM=openrouter
       if [ -z "$OLD_OPENROUTER" ]; then guide_openrouter; fi
       OPENROUTER_KEY="$(get_openrouter_key "$OLD_OPENROUTER")";;
    4) if command -v claude >/dev/null; then LLM=claude-cli
       else echo "The 'claude' command was not found on your PATH."; guide_claude_cli
       read -r -p "Press Enter to go back to the menu..." _; echo; fi;;
    *) echo "Please enter 1, 2, 3 or 4."; echo;;
  esac
done

if [ "$LLM" = "openrouter" ]; then
  echo "OpenRouter model ids look like provider/model (browse https://openrouter.ai/models)."
  read -r -p "Model (press Enter for anthropic/claude-sonnet-4.5): " MODEL
else
  read -r -p "Model override (press Enter for the default): " MODEL
fi

umask 077
{
  echo "TUTOR_LLM=$LLM"
  [ -n "$ANTHROPIC_KEY" ] && echo "ANTHROPIC_API_KEY='$ANTHROPIC_KEY'"
  [ -n "$OPENAI_KEY" ] && echo "OPENAI_API_KEY='$OPENAI_KEY'"
  [ -n "$OPENROUTER_KEY" ] && echo "OPENROUTER_API_KEY='$OPENROUTER_KEY'"
  [ -n "$MODEL" ] && echo "TUTOR_MODEL='$MODEL'"
  true
} > .env
chmod 600 .env

echo
echo "Saved to .env (readable only by you). LLM: $LLM"
echo "Setup complete. Start the tutor with: ./launch.sh"
