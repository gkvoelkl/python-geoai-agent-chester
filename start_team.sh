#!/usr/bin/env bash
#
# start_team.sh — start Chester-Team: the orchestrator's gateway and a dashboard on it.
#
#   gateway    gateway_team.py  — orchestrator + WebChatChannel (SSE), team.webchat_port (8100)
#   dashboard  dashboard.py     — the same SelmaKit web UI, team.dashboard_port (8601)
#
# The counterpart of start.sh. Ports come from the `team` block of
# .chester/chester.json, so agent and team can run side by side; the team never starts
# Telegram (two bots cannot share one token). Ctrl-C stops both.
#
# Usage:
#   ./start_team.sh            # start gateway + dashboard
#   ./start_team.sh --no-open  # do not auto-open the dashboard in the browser
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

# --- Ports from the team block, Ollama URL from the model block --------------
read -r SRV_PORT DASHBOARD_PORT OLLAMA_URL < <(
  uv run python - <<'PY'
from chester.runtime.config import CONFIG_NAME, STATE_DIR
from chester.team.orchestrator import team_block
from selmakit.config import load_config
team = team_block()
print(team["webchat_port"], team["dashboard_port"],
      load_config(STATE_DIR, config_name=CONFIG_NAME).model.effective_base_url)
PY
)
SERVER_URL="http://localhost:${SRV_PORT}"
DASHBOARD_URL="http://localhost:${DASHBOARD_PORT}"

OLLAMA_ROOT="${OLLAMA_URL%/v1}"
if ! curl -sf -o /dev/null --max-time 2 "${OLLAMA_ROOT}/api/tags"; then
  echo "⚠️  Ollama scheint unter ${OLLAMA_ROOT} nicht erreichbar zu sein."
  echo "   Starte Ollama (z. B. 'ollama serve') — die Dienste laufen trotzdem an."
fi
for port in "${SRV_PORT}" "${DASHBOARD_PORT}"; do
  if lsof -ti ":${port}" >/dev/null 2>&1; then
    echo "❌ Port ${port} ist belegt — läuft Chester-Team schon?" >&2
    echo "   Andere Ports setzen: team.webchat_port / team.dashboard_port in .chester/chester.json" >&2
    exit 1
  fi
done

PIDS=()
cleanup() {
  trap - INT TERM EXIT
  echo
  echo "⏹  Stoppe Chester-Team…"
  for pid in "${PIDS[@]:-}"; do
    [ -n "${pid}" ] && kill "${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "🛰  Team-Gateway:   ${SERVER_URL}"
uv run gateway_team.py &
PIDS+=("$!")

for _ in $(seq 1 40); do
  if curl -sf -o /dev/null --max-time 1 "${SERVER_URL}/docs"; then break; fi
  sleep 0.5
done

echo "🌍 Team-Dashboard: ${DASHBOARD_URL}"
CHESTER_AGENT=team CHESTER_GATEWAY_URL="${SERVER_URL}" uv run streamlit run dashboard.py \
  --server.port "${DASHBOARD_PORT}" \
  --server.headless "$([ "$OPEN_FLAG" = "true" ] && echo false || echo true)" \
  --browser.gatherUsageStats false &
PIDS+=("$!")

wait
