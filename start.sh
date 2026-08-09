#!/usr/bin/env bash
#
# start.sh — start Chester's gateway and dashboard.
#
#   gateway    gateway.py          — agent backend + WebChatChannel (SSE), :8000
#   dashboard  dashboard.py        — SelmaKit Streamlit web UI, :8501
#   phoenix    arizephoenix/phoenix (Docker) — OTel collector + UI, :6006/:4317
#
# The dashboard talks to the gateway over /webchat/stream. The gateway exports
# OpenTelemetry spans to the Phoenix container on :4317. All are started here;
# Ctrl-C stops them.
#
# Usage:
#   ./start.sh               # start phoenix (if Docker) + gateway + dashboard
#   ./start.sh --no-open     # do not auto-open the dashboard in the browser
#   ./start.sh --no-phoenix  # skip the Phoenix tracing container
#                            # (or set CHESTER_NO_PHOENIX=1)
#
set -euo pipefail

cd "$(dirname "$0")"

DASHBOARD_PORT="${CHESTER_DASHBOARD_PORT:-8501}"
PHOENIX_CONTAINER="chester-phoenix"
OPEN_FLAG="true"
# Tracing collector on by default; opt out with --no-phoenix or CHESTER_NO_PHOENIX.
PHOENIX_FLAG="$([ -n "${CHESTER_NO_PHOENIX:-}" ] && echo false || echo true)"
for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN_FLAG="false" ;;
    --no-phoenix) PHOENIX_FLAG="false" ;;
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
  docker stop "$PHOENIX_CONTAINER" >/dev/null 2>&1 || true
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# --- Phoenix tracing collector (optional, needs Docker) ---------------------
# The gateway exports OTel spans over OTLP/gRPC to localhost:4317. Phoenix runs
# as a standalone container providing that endpoint (UI on :6006); it can't be a
# Python dep because arize-phoenix pins pydantic-ai-slim<2. Skipped if Docker is
# absent or --no-phoenix / CHESTER_NO_PHOENIX is set — the gateway then runs
# without a collector (and logs harmless OTLP send failures).
if [ "$PHOENIX_FLAG" = "true" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "⚠️  docker nicht gefunden — Phoenix übersprungen, Gateway läuft ohne Tracing."
  elif ! docker info >/dev/null 2>&1; then
    echo "⚠️  Docker-Daemon nicht erreichbar — Phoenix übersprungen, Gateway läuft ohne Tracing."
  else
    echo "🔭 Phoenix:   http://localhost:6006  (OTLP: localhost:4317)"
    docker rm -f "$PHOENIX_CONTAINER" >/dev/null 2>&1 || true
    # Non-fatal: a failed collector start must not abort Chester (set -e).
    if ! docker run -d --rm --name "$PHOENIX_CONTAINER" \
      -p 6006:6006 -p 4317:4317 \
      arizephoenix/phoenix:latest >/dev/null 2>&1; then
      echo "⚠️  Phoenix-Container konnte nicht starten — Gateway läuft ohne Tracing."
    fi
  fi
fi

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
