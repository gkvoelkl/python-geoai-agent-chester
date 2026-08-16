#!/usr/bin/env bash
# Der eine Prüfbefehl — für Hook, Mensch und Agent gleichermaßen.
#
# Ohne genau einen Einstiegspunkt entstehen mehrere Definitionen von „grün": die des
# Hooks, die des Dauerlaufs und die im Kopf des Entwicklers. Es gibt bewusst keine CI
# (siehe Phase H, H0b) — dieses Skript ist die Prüfung.
#
#   ./check.sh          schnelle Runde: Lint + Strukturtests + Unit-Tests (~1 min)
#   ./check.sh --full   zusätzlich die Netz-/QGIS-Schichten (langsam, braucht Internet)
#   ./check.sh --evals  zusätzlich die Eval-Bank (braucht Ollama, dauert lange)
#
# Was dieses Skript NICHT leisten kann: den Nachweis, dass das Repo auf einem fremden
# Rechner aus dem Lock baut. Dafür bräuchte es eine unabhängige Umgebung; vor einer
# Veröffentlichung also einmal von Hand: frischer Klon, `uv sync --frozen`, `./check.sh`.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

full=0; evals=0
for arg in "$@"; do
  case "$arg" in
    --full) full=1 ;;
    --evals) evals=1 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unbekanntes Argument: $arg" >&2; exit 2 ;;
  esac
done

failed=()
step() {
  local name="$1"; shift
  printf '\n\033[1m▸ %s\033[0m\n' "$name"
  if "$@"; then printf '  ✓ %s\n' "$name"; else failed+=("$name"); printf '  ✗ %s\n' "$name"; fi
}

# Lint wird über die *Ratsche* geprüft (in den Strukturtests), nicht gegen null:
# das Repo trägt 61 Altbefunde, ein Schritt `ruff check .` wäre dauerhaft rot — und
# ein dauerhaft roter Prüfbefehl bringt bei, ihn zu ignorieren. Hier nur die Zahl.
printf '\n\033[1m▸ Lint (informativ)\033[0m\n'
lint_now=$(uv run ruff check --output-format=concise . 2>/dev/null | grep -c '\.py:' || true)
lint_base=$(uv run python -c "import json;print(json.load(open('tests/structure_baseline.json'))['ruff_total'])" 2>/dev/null || echo '?')
printf '  ruff:  %s Befunde (Baseline %s)\n' "$lint_now" "$lint_base"
mypy_now=$(uv run mypy chester 2>/dev/null | grep -cE '^[[:alnum:]_/.]+\.py:[0-9]+: error:' || true)
mypy_base=$(uv run python -c "import json;print(sum(json.load(open('tests/structure_baseline.json'))['mypy_errors'].values()))" 2>/dev/null || echo '?')
printf '  mypy:  %s Fehler (Baseline %s) — Ratschen prueft der Strukturtest\n' "$mypy_now" "$mypy_base"

step "Strukturtests"   uv run pytest tests/test_structure.py -q
step "Unit-Tests"      uv run pytest -q
[ "$full" = 1 ]  && step "Netz + QGIS" uv run pytest -q --run-network
[ "$evals" = 1 ] && step "Eval-Bank"   uv run evals.py --gate

printf '\n'
if [ ${#failed[@]} -eq 0 ]; then
  printf '\033[32mgrün\033[0m — alle Prüfungen bestanden\n'
  exit 0
fi
printf '\033[31mrot\033[0m — fehlgeschlagen: %s\n' "${failed[*]}"
printf 'Zu tun: den ersten fehlgeschlagenen Schritt einzeln laufen lassen und beheben.\n'
exit 1
