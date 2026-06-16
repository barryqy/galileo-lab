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
  echo ""
  echo "GALILEO_API_KEY is not set."
  echo "Create a Galileo API key, then run:"
  echo '  export GALILEO_API_KEY="..."'
  echo "  source 0-init-lab.sh"
  return 1 2>/dev/null || exit 1
fi

python3 galileo_lab.py env
