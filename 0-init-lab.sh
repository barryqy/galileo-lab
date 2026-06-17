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

fetch_galileo_key_service(){
  local service_url="${GALILEO_KEY_SERVICE_URL:-https://ks.barrysecure.com/credentials}"
  local lab_id="${GALILEO_KEY_SERVICE_LAB_ID:-galileo}"
  local lab_password="${GALILEO_LAB_PASSWORD:-${LAB_PASSWORD:-${DEVENV_PASSWORD:-}}}"
  local response_file=".galileo/key-service-response.json"

  if [ -z "$lab_password" ]; then
    return 1
  fi

  if ! curl -fsS "$service_url" \
      -H "X-Lab-ID: $lab_id" \
      -H "X-Session-Password: $lab_password" \
      -o "$response_file"; then
    rm -f "$response_file"
    return 1
  fi

  GALILEO_API_KEY="$(python3 - <<'PY'
import json
from pathlib import Path

data = json.loads(Path(".galileo/key-service-response.json").read_text(encoding="utf-8"))
print(data.get("GALILEO_API_KEY") or data.get("galileo_api_key") or "")
PY
)"

  rm -f "$response_file"
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
  if fetch_galileo_key_service; then
    echo "Credentials ready"
  else
    echo ""
    echo "GALILEO_API_KEY is not available from key-service."
    echo "Ask the lab instructor to verify the Galileo key-service entry and lab session secret."
    return 1 2>/dev/null || exit 1
  fi
fi

python3 galileo_lab.py env
