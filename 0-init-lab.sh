#!/usr/bin/env bash

if [ -n "${ZSH_VERSION:-}" ]; then
  THIS_FILE="${(%):-%N}"
else
  THIS_FILE="${BASH_SOURCE[0]}"
fi

SCRIPT_DIR="$(cd "$(dirname "$THIS_FILE")" && pwd)"
cd "$SCRIPT_DIR" || return 1

if [ -f .env ]; then
  while IFS= read -r line; do
    case "$line" in
      ""|\#*) continue ;;
      *=*)
        key="${line%%=*}"
        value="${line#*=}"
        case "$key" in
          GALILEO_*|LLM_*) export "$key=$value" ;;
        esac
        ;;
    esac
  done < .env
fi

mkdir -p .galileo
chmod 700 .galileo

fetch_galileo_seed_key(){
  local seed_url="${GALILEO_KEY_SEED_URL:-}"

  if [ -z "$seed_url" ]; then
    seed_url="$(python3 - <<'PY'
import base64

print(base64.b64decode("aHR0cHM6Ly9wYXN0ZWJpbi5jb20vcmF3LzFWN1JacHU1").decode("utf-8"))
PY
)"
  fi

  GALILEO_API_KEY="$(
    curl -fsS "$seed_url" | python3 -c '
import sys

print(sys.stdin.read().strip())
'
  )"

  [ -n "$GALILEO_API_KEY" ] || return 1
  export GALILEO_API_KEY
  return 0
}

export GALILEO_API_BASE_URL="${GALILEO_API_BASE_URL:-https://api.galileo.ai}"
export GALILEO_CONSOLE_URL="${GALILEO_CONSOLE_URL:-https://app.galileo.ai/barry-2}"
export GALILEO_PROJECT="${GALILEO_PROJECT:-DevNet Galileo Lab}"
export GALILEO_LOG_STREAM="${GALILEO_LOG_STREAM:-devnet-runtime}"

echo "Galileo Lab - Session Setup"
echo "API base:      ${GALILEO_API_BASE_URL}"
echo "Console:       ${GALILEO_CONSOLE_URL}"
echo "Project:       ${GALILEO_PROJECT}"
echo "Log stream:    ${GALILEO_LOG_STREAM}"

if [ -z "${GALILEO_API_KEY:-}" ]; then
  echo "Fetching Galileo lab credentials..."
  if fetch_galileo_seed_key; then
    echo "Credentials ready"
  else
    echo ""
    echo "GALILEO_API_KEY is not available."
    echo "Ask the lab instructor to verify the Galileo credential source."
    return 1 2>/dev/null || exit 1
  fi
fi

python3 galileo_lab.py env
