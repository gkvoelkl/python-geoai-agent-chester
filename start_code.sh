#!/usr/bin/env bash
#
# start_code.sh — start Chester-Code: the coder's gateway and a dashboard on it.
#
#   gateway    gateway_code.py  — coder + WebChatChannel (SSE), code.webchat_port (8200)
#   dashboard  dashboard.py     — the same SelmaKit web UI, code.dashboard_port (8701)
#
# The counterpart of start.sh and start_team.sh. Ports come from the `code` block of
# .chester/chester.json, so agent, team and coder can run side by side; the coder never
# starts Telegram (two bots cannot share one token). Ctrl-C stops both.
#
# Usage:
#   ./start_code.sh            # start gateway + dashboard
#   ./start_code.sh --no-open  # do not auto-open the dashboard in the browser
#
set -euo pipefail

cd "$(dirname "$0")"

OPEN_FLAG="true"
for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN_FLAG="false" ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# --- Ports from the code block, model endpoint from the model block ----------
read -r SRV_PORT DASHBOARD_PORT MODEL_URL < <(
  uv run python - <<'PY'
from chester.code.coder import code_block
from chester.runtime.config import CONFIG_NAME, STATE_DIR
from selmakit.config import load_config
code = code_block()
print(code["webchat_port"], code["dashboard_port"],
      load_config(STATE_DIR, config_name=CONFIG_NAME).model.effective_base_url)
PY
)
SERVER_URL="http://localhost:${SRV_PORT}"
DASHBOARD_URL="http://localhost:${DASHBOARD_PORT}"

# `/models` answers on any OpenAI-compatible server — llama.cpp and Ollama alike.
if ! curl -sf -o /dev/null --max-time 2 "${MODEL_URL%/}/models"; then
  echo "⚠️  Der Modellserver scheint unter ${MODEL_URL} nicht erreichbar zu sein."
  echo "   Starte ihn (z. B. ~/Desktop/git/sh-llama.cpp/start.sh) — die Dienste laufen trotzdem an."
fi
for port in "${SRV_PORT}" "${DASHBOARD_PORT}"; do
  if lsof -ti ":${port}" >/dev/null 2>&1; then
    echo "❌ Port ${port} ist belegt — läuft Chester-Code schon?" >&2
    echo "   Andere Ports setzen: code.webchat_port / code.dashboard_port in .chester/chester.json" >&2
    exit 1
  fi
done

PIDS=()
cleanup() {
  trap - INT TERM EXIT
  echo
  echo "⏹  Stoppe Chester-Code…"
  for pid in "${PIDS[@]:-}"; do
    [ -n "${pid}" ] && kill "${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "🛰  Code-Gateway:   ${SERVER_URL}"
uv run gateway_code.py &
PIDS+=("$!")

for _ in $(seq 1 40); do
  if curl -sf -o /dev/null --max-time 1 "${SERVER_URL}/docs"; then break; fi
  sleep 0.5
done

echo "🌍 Code-Dashboard: ${DASHBOARD_URL}"
CHESTER_AGENT=code CHESTER_GATEWAY_URL="${SERVER_URL}" uv run streamlit run dashboard.py \
  --server.port "${DASHBOARD_PORT}" \
  --server.headless "$([ "$OPEN_FLAG" = "true" ] && echo false || echo true)" \
  --browser.gatherUsageStats false &
PIDS+=("$!")

wait
