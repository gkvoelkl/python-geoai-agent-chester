#!/usr/bin/env bash
#
# start.sh — start Chester's gateway and dashboard.
#
#   gateway    gateway.py          — agent backend + WebChatChannel (SSE), :8000
#   dashboard  dashboard.py        — SelmaKit Streamlit web UI, :8501
#
# The dashboard talks to the gateway over /webchat/stream. Both are started
# here; Ctrl-C stops them. No tracing collector is started: since selmakit
# 0.1.26 tracing is opt-in through the `tracing` block in .chester/chester.json
# and needs whatever OTLP/HTTP collector you point it at.
#
# Usage:
#   ./start.sh               # start gateway + dashboard
#   ./start.sh --no-open     # do not auto-open the dashboard in the browser
#
set -euo pipefail

cd "$(dirname "$0")"

DASHBOARD_PORT="${CHESTER_DASHBOARD_PORT:-8501}"
OPEN_FLAG="true"
for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN_FLAG="false" ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# --- Gateway URL + Ollama base URL from the config --------------------------
read -r SRV_HOST SRV_PORT OLLAMA_URL < <(
  uv run python - <<'PY'
from agent_build import CONFIG_NAME, STATE_DIR
from selmakit.config import load_config
cfg = load_config(STATE_DIR, config_name=CONFIG_NAME)
wc = cfg.channels.webchat
host = "localhost" if wc.host in ("0.0.0.0", "") else wc.host
print(host, wc.port, cfg.model.effective_base_url)
PY
)
SERVER_URL="http://${SRV_HOST}:${SRV_PORT}"
DASHBOARD_URL="http://localhost:${DASHBOARD_PORT}"

# --- Preflight: warn if Ollama is unreachable (the agent needs it) ----------
OLLAMA_ROOT="${OLLAMA_URL%/v1}"
if ! curl -sf -o /dev/null --max-time 2 "${OLLAMA_ROOT}/api/tags"; then
  echo "⚠️  Ollama scheint unter ${OLLAMA_ROOT} nicht erreichbar zu sein."
  echo "   Starte Ollama (z. B. 'ollama serve') — die Dienste laufen trotzdem an."
fi

# --- Stop both children on exit ---------------------------------------------
PIDS=()
cleanup() {
  trap - INT TERM EXIT
  echo
  echo "⏹  Stoppe Chester…"
  for pid in "${PIDS[@]:-}"; do
    [ -n "${pid}" ] && kill "${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# --- Gateway (gateway.py) ---------------------------------------------------
echo "🛰  Gateway:   ${SERVER_URL}"
uv run gateway.py &
PIDS+=("$!")

# Wait for the gateway to accept requests before starting the dashboard.
for _ in $(seq 1 40); do
  if curl -sf -o /dev/null --max-time 1 "${SERVER_URL}/docs"; then break; fi
  sleep 0.5
done

# --- Dashboard (dashboard.py via Streamlit) ---------------------------------
echo "🌍 Dashboard: ${DASHBOARD_URL}"
uv run streamlit run dashboard.py \
  --server.port "${DASHBOARD_PORT}" \
  --server.headless "$([ "$OPEN_FLAG" = "true" ] && echo false || echo true)" \
  --browser.gatherUsageStats false &
PIDS+=("$!")

wait
