#!/usr/bin/env bash
#
# test.sh — start Chester's test bench (test_app.py).
#
#   test bench  test_app.py  — SelmaKit Streamlit UI: run / edit / history, :8501
#
# A thin skin over the same machinery as the CLI (testprompt.py / evals.py):
# pick a test prompt, run it fresh/EN/judged, watch the tool exchange stream
# live, then inspect answer + rendered map + verdict, or browse eval history.
#
# Usage:
#   ./test.sh                          # start the test bench and open it in the browser
#   ./test.sh --no-open                # do not auto-open the browser
#   CHESTER_EVAL_CELL=L+ ./test.sh     # label every judged run with its measurement cell
#
set -euo pipefail

cd "$(dirname "$0")"

BENCH_PORT="${CHESTER_TEST_PORT:-8501}"
OPEN_FLAG="true"
for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN_FLAG="false" ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

BENCH_URL="http://localhost:${BENCH_PORT}"
echo "🧪 Test bench: ${BENCH_URL}"

# Say the measurement cell before the bench comes up, not in the report afterwards:
# a night of runs started without the label cannot be assigned later, because the
# cell is set, never derived (`chester/evalcells.py`). Same warning as `evals.py`.
if [ -n "${CHESTER_EVAL_CELL:-}" ]; then
  echo "   Cell: ${CHESTER_EVAL_CELL}"
else
  echo "   ⚠ No CHESTER_EVAL_CELL set — runs are archived as *unknown* cell."
fi

# A busy port is almost always a bench that is still running — say so, rather than
# letting streamlit fail with a bare address-in-use traceback.
if lsof -ti ":${BENCH_PORT}" >/dev/null 2>&1; then
  echo "❌ Port ${BENCH_PORT} is busy — a bench is probably still running." >&2
  echo "   Stop it with: pkill -f 'streamlit run test_app'" >&2
  echo "   Or use another port: CHESTER_TEST_PORT=8502 ./test.sh" >&2
  exit 1
fi

uv run streamlit run test_app.py \
  --server.port "${BENCH_PORT}" \
  --server.headless "$([ "$OPEN_FLAG" = "true" ] && echo false || echo true)" \
  --browser.gatherUsageStats false
