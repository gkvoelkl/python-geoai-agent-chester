#!/usr/bin/env bash
#
# test_code.sh — the test bench (test_app.py) against Chester-Code's coder.
#
# The counterpart of test.sh and test_team.sh: the same bench, but `CHESTER_AGENT=code`
# makes `agents.build_agent` build the coder, and every archived run records
# `agent: code` (chester/evalcells.py) — so the architectures never average together in
# one history. Port from `code.bench_port` (default 8702), clear of the other benches.
#
# Usage:
#   ./test_code.sh                          # start the bench and open it in the browser
#   ./test_code.sh --no-open                # do not auto-open the browser
#   CHESTER_EVAL_CELL=CC-1 ./test_code.sh   # label every judged run with its cell
#   CHESTER_JUDGE=result ./test_code.sh     # the judge grades the result only
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
  'from chester.code.coder import code_block; print(code_block()["bench_port"])')}"
BENCH_URL="http://localhost:${BENCH_PORT}"
echo "🧪 Code test bench: ${BENCH_URL}  (agent: code)"

if [ -n "${CHESTER_EVAL_CELL:-}" ]; then
  echo "   Cell: ${CHESTER_EVAL_CELL}"
else
  echo "   ⚠ No CHESTER_EVAL_CELL set — runs are archived as *unknown* cell."
fi
[ "${CHESTER_JUDGE:-}" = "result" ] && echo "   Judge: result only"

if lsof -ti ":${BENCH_PORT}" >/dev/null 2>&1; then
  echo "❌ Port ${BENCH_PORT} is busy — a code bench is probably still running." >&2
  echo "   Or use another port: CHESTER_TEST_PORT=8703 ./test_code.sh" >&2
  exit 1
fi

CHESTER_AGENT=code uv run streamlit run test_app.py \
  --server.port "${BENCH_PORT}" \
  --server.headless "$([ "$OPEN_FLAG" = "true" ] && echo false || echo true)" \
  --browser.gatherUsageStats false
