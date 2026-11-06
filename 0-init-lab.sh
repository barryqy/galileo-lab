#!/usr/bin/env bash

if [ -n "${ZSH_VERSION:-}" ]; then
  THIS_FILE="${(%):-%N}"
else
  THIS_FILE="${BASH_SOURCE[0]}"
fi

SCRIPT_DIR="$(cd "$(dirname "$THIS_FILE")" && pwd)"
cd "$SCRIPT_DIR" || return 1

if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi

# Restore the caller's legacy settings before .env gets a chance to override them.
if [ -n "${_GALILEO_ACTIVE_PROFILE:-}" ]; then
  export GALILEO_API_BASE_URL="$_GALILEO_LEGACY_API_BASE_URL"
  export GALILEO_CONSOLE_URL="$_GALILEO_LEGACY_CONSOLE_URL"
  export GALILEO_PROJECT="$_GALILEO_LEGACY_PROJECT"
  export GALILEO_LOG_STREAM="$_GALILEO_LEGACY_LOG_STREAM"
  export GALILEO_STATE_DIR="$_GALILEO_LEGACY_STATE_DIR"
  export GALILEO_PROJECT_ID="$_GALILEO_LEGACY_PROJECT_ID"
  export GALILEO_LOG_STREAM_ID="$_GALILEO_LEGACY_LOG_STREAM_ID"
  export GALILEO_KEY_SEED_URL="$_GALILEO_LEGACY_KEY_SEED_URL"
fi

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

export GALILEO_LAB_PROFILE="${1:-}"
if [ "${1:-}" = "observability" ] || [ "${1:-}" = "workshop" ]; then
  if [ -z "${_GALILEO_ACTIVE_PROFILE:-}" ]; then
    _GALILEO_LEGACY_API_BASE_URL="${GALILEO_API_BASE_URL:-https://api.galileo.ai}"
    if [ -n "${GALILEO_LAB_GATEWAY_URL:-}" ] && [ "$_GALILEO_LEGACY_API_BASE_URL" = "$GALILEO_LAB_GATEWAY_URL" ]; then
      _GALILEO_LEGACY_API_BASE_URL="https://api.galileo.ai"
    fi
    _GALILEO_LEGACY_CONSOLE_URL="${GALILEO_CONSOLE_URL:-https://app.galileo.ai/barry-2}"
    _GALILEO_LEGACY_PROJECT="${GALILEO_PROJECT:-DevNet Galileo Lab}"
    _GALILEO_LEGACY_LOG_STREAM="${GALILEO_LOG_STREAM:-devnet-runtime}"
    _GALILEO_LEGACY_STATE_DIR="${GALILEO_STATE_DIR:-.galileo}"
    _GALILEO_LEGACY_PROJECT_ID="${GALILEO_PROJECT_ID:-}"
    _GALILEO_LEGACY_LOG_STREAM_ID="${GALILEO_LOG_STREAM_ID:-}"
    _GALILEO_LEGACY_KEY_SEED_URL="${GALILEO_KEY_SEED_URL:-}"
    export _GALILEO_LEGACY_API_BASE_URL _GALILEO_LEGACY_CONSOLE_URL _GALILEO_LEGACY_PROJECT
    export _GALILEO_LEGACY_LOG_STREAM _GALILEO_LEGACY_STATE_DIR _GALILEO_LEGACY_PROJECT_ID
    export _GALILEO_LEGACY_LOG_STREAM_ID _GALILEO_LEGACY_KEY_SEED_URL
  fi
fi

if [ "${1:-}" = "observability" ]; then
  _GALILEO_ACTIVE_PROFILE="observability"
  export GALILEO_API_BASE_URL="https://api.multitenant.galileocloud.io"
  export GALILEO_CONSOLE_URL="https://console.multitenant.galileocloud.io/splunkse"
  export GALILEO_PROJECT="AI Observability Lab"
  export GALILEO_LOG_STREAM="ai-observability-lab"
  export GALILEO_PROJECT_ID="59f3d09d-c104-4f40-a3b8-77af51ea55d1"
  export GALILEO_LOG_STREAM_ID="3d95e2c0-38cd-4c1a-bdd8-7a259de8a33b"
  export GALILEO_STATE_DIR=".galileo/observability"
  if [ -z "${GALILEO_API_KEY:-}" ] && [ -z "${GALILEO_OBSERVABILITY_KEY_SEED_URL:-}" ]; then
    echo "Ask the lab instructor to enable the AI Observability Lab credential for this session."
    return 1 2>/dev/null || exit 1
  fi
  if [ -n "${GALILEO_OBSERVABILITY_KEY_SEED_URL:-}" ]; then
    export GALILEO_KEY_SEED_URL="$GALILEO_OBSERVABILITY_KEY_SEED_URL"
  fi
elif [ "${1:-}" = "workshop" ]; then
  _GALILEO_ACTIVE_PROFILE="workshop"
  export GALILEO_API_BASE_URL="https://api.multitenant.galileocloud.io"
  export GALILEO_CONSOLE_URL="https://console.multitenant.galileocloud.io/workshop"
  export GALILEO_PROJECT="AI Observability Lab"
  export GALILEO_LOG_STREAM="ai-observability-lab"
  export GALILEO_PROJECT_ID="2e652072-412e-4bef-97a5-1a8f4dc5b68e"
  export GALILEO_LOG_STREAM_ID="9ac1ae85-1c47-4447-8050-df5fbb5b2ae6"
  export GALILEO_STATE_DIR=".galileo/workshop"
  if [ -n "${GALILEO_WORKSHOP_KEY_SEED_URL:-}" ]; then
    export GALILEO_KEY_SEED_URL="$GALILEO_WORKSHOP_KEY_SEED_URL"
  fi
elif [ -n "${1:-}" ]; then
  echo "Unknown Galileo lab profile: $1"
  return 1 2>/dev/null || exit 1
else
  _GALILEO_ACTIVE_PROFILE=""
fi

export _GALILEO_ACTIVE_PROFILE

if [ "${GALILEO_LAB_PROFILE:-}" != "workshop" ] || [ -n "${GALILEO_API_KEY:-}" ] \
    || [ -n "${GALILEO_WORKSHOP_KEY_SEED_URL:-}" ]; then
  unset GALILEO_LAB_SESSION_TOKEN GALILEO_LAB_GATEWAY_URL GALILEO_LAB_SESSION_EXPIRES_AT
  unset GALILEO_LAB_SESSION_PROJECT_ID GALILEO_LAB_SESSION_LOG_STREAM_ID GALILEO_LAB_SESSION_ENVIRONMENT_ID
  unset GALILEO_LAB_SESSION_RENEWAL_TOKEN
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

if [ -z "${GALILEO_API_KEY:-}" ] && [ "${GALILEO_LAB_PROFILE:-}" = "workshop" ] \
    && [ -z "${GALILEO_WORKSHOP_KEY_SEED_URL:-}" ]; then
  session_fields="$(python3 "$SCRIPT_DIR/galileo_session.py")" || {
    echo "Ask the lab instructor to check this Workshop session."
    return 1 2>/dev/null || exit 1
  }
  while IFS= read -r line; do
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      GALILEO_LAB_SESSION_TOKEN|GALILEO_LAB_SESSION_RENEWAL_TOKEN|GALILEO_LAB_GATEWAY_URL|GALILEO_LAB_SESSION_EXPIRES_AT|GALILEO_LAB_SESSION_PROJECT_ID|GALILEO_LAB_SESSION_LOG_STREAM_ID|GALILEO_LAB_SESSION_ENVIRONMENT_ID)
        export "$key=$value" ;;
    esac
  done <<EOF
$session_fields
EOF
  unset session_fields line key value
  export GALILEO_API_BASE_URL="$GALILEO_LAB_GATEWAY_URL"
elif [ -z "${GALILEO_API_KEY:-}" ]; then
  if ! fetch_galileo_seed_key; then
    echo ""
    echo "GALILEO_API_KEY is not available."
    echo "Ask the lab instructor to verify the Galileo credential source."
    return 1 2>/dev/null || exit 1
  fi
fi

echo "Galileo lab session is ready."
