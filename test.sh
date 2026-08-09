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
#   ./test.sh              # start the test bench and open it in the browser
#   ./test.sh --no-open    # do not auto-open the browser
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

uv run streamlit run test_app.py \
  --server.port "${BENCH_PORT}" \
  --server.headless "$([ "$OPEN_FLAG" = "true" ] && echo false || echo true)" \
  --browser.gatherUsageStats false
