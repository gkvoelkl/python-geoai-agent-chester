#!/usr/bin/env bash
#
# test_team.sh — the test bench (test_app.py) against Chester-Team's orchestrator.
#
# The counterpart of test.sh: the same bench, but `CHESTER_AGENT=team` makes
# `agents.build_agent` build the orchestrator, and every archived run records
# `agent: team` (chester/evalcells.py) — so agent and team never average together in
# one history. Port from `team.bench_port` (default 8602), clear of test.sh.
#
# Usage:
#   ./test_team.sh                          # start the bench and open it in the browser
#   ./test_team.sh --no-open                # do not auto-open the browser
#   CHESTER_EVAL_CELL=L+ ./test_team.sh     # label every judged run with its cell
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

BENCH_PORT="${CHESTER_TEST_PORT:-$(uv run python -c \
  'from chester.team.orchestrator import team_block; print(team_block()["bench_port"])')}"
BENCH_URL="http://localhost:${BENCH_PORT}"
echo "🧪 Team test bench: ${BENCH_URL}  (agent: team)"

if [ -n "${CHESTER_EVAL_CELL:-}" ]; then
  echo "   Cell: ${CHESTER_EVAL_CELL}"
else
  echo "   ⚠ No CHESTER_EVAL_CELL set — runs are archived as *unknown* cell."
fi

if lsof -ti ":${BENCH_PORT}" >/dev/null 2>&1; then
  echo "❌ Port ${BENCH_PORT} is busy — a team bench is probably still running." >&2
  echo "   Or use another port: CHESTER_TEST_PORT=8603 ./test_team.sh" >&2
  exit 1
fi

CHESTER_AGENT=team uv run streamlit run test_app.py \
  --server.port "${BENCH_PORT}" \
  --server.headless "$([ "$OPEN_FLAG" = "true" ] && echo false || echo true)" \
  --browser.gatherUsageStats false
