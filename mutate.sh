#!/usr/bin/env bash
# Mutationstests — der Sensor auf den Sensor.
#
# Chesters ~400 Tests sind größtenteils agentengeschrieben und zugleich der
# Behaviour-Sensor des Projekts: eine Prüfinstanz, deren eigene Verlässlichkeit
# ungeprüft ist. Abdeckung misst, welche Zeilen *ausgeführt* werden — nicht, ob ein
# Test es merkt, wenn sie falsch sind. Genau diese Lücke misst ein Mutations-Score.
#
# Bewusst NICHT im Dauerlauf und nicht in ./check.sh: der Lauf dauert Minuten, und
# auch im Praxisbericht lief er inkrementell von Hand.
#
# ACHTUNG — mutmut 2.x mutiert die Quelldateien **an Ort und Stelle** und stellt sie
# erst danach wieder her. Solange ein Lauf aktiv ist, dürfen KEINE anderen Prüfungen
# laufen: `mypy`, `ruff`, `pytest` und `harenessa` messen sonst gegen einen gerade
# eingesetzten Mutanten und melden Phantom-Regressionen (beobachtet: mypy 65 → 67).
# Ebenso wenig parallel committen. Bricht ein Lauf ab, `git diff chester/` prüfen.
#
#   ./mutate.sh              alle reinen Kernmodule
#   ./mutate.sh geofacts     nur eines
#
# Beschränkt auf die reinen Kerne: schnell, deterministisch, kein QGIS, kein Netz.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

# Modul → die Tests, die es wirklich abdecken. Nicht `tests/test_<modul>.py` raten:
# `geofacts` und `plausibility` haben gar keine gleichnamige Datei, ihre Tests liegen
# verstreut. Ein zu enger Runner meldet Mutanten als überlebend, die längst geprüft
# sind; ein zu weiter (die ganze Suite, 45 s) macht den Lauf unbezahlbar.
tests_for() {
  case "$1" in
    # test_qgis_capability.py gehoert dazu: `measure_layer` wird NUR dort geprueft.
    # Fehlt es im Runner, melden dessen Mutanten "ueberlebt", obwohl Tests existieren
    # - der Score misst dann die Zuordnungstabelle, nicht die Testguete.
    geofacts)     echo "tests/test_validation_v1.py tests/test_validation_v5.py tests/test_topology.py tests/test_geocache.py tests/test_qgis_capability.py" ;;
    adminlevels)  echo "tests/test_adminlevels.py" ;;
    plausibility) echo "tests/test_validation_v1.py" ;;
    geocache)     echo "tests/test_geocache.py tests/test_geocache_ttl.py tests/test_provenance.py tests/test_commands.py" ;;
    *)            echo "tests" ;;
  esac
}

if [ $# -gt 0 ]; then modules=("$@"); else
  modules=(geofacts adminlevels plausibility geocache); fi

for m in "${modules[@]}"; do
  printf '\n\033[1m▸ %s\033[0m  (Tests: %s)\n' "$m" "$(tests_for "$m")"
  # mutmut<3 bewusst gepinnt: 3.x kopiert die Quellen in ein `mutants/`-Verzeichnis
  # und zerlegt dabei den Paketimport (`No module named chester.qgis_env`); 2.x
  # mutiert an Ort und Stelle und nimmt die Pfade als Argumente.
  uvx --from 'mutmut<3' mutmut run \
      --paths-to-mutate "chester/${m}.py" \
      --tests-dir tests \
      --runner "uv run pytest -q -x $(tests_for "$m")" \
      --simple-output 2>&1 | tr '\r' '\n' | tail -2
done

printf '\nErgebnis deuten: **überlebende Mutanten** sind Stellen, an denen der Code\n'
printf 'geändert werden kann, ohne dass ein Test es merkt — dort fehlt eine Zusicherung,\n'
printf 'nicht unbedingt ein Test. Den Stand in internal/technical-debt.md eintragen.\n'
printf 'Ueberlebende ansehen: `uvx --from "mutmut<3" mutmut results` bzw. `... show <id>`.\n'
