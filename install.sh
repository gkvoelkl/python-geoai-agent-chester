#!/usr/bin/env bash
#
# install.sh — comfortable one-shot installer for Chester.
#
# Walks through everything needed to go from a fresh clone to a runnable agent:
#
#   1. uv          — the Python package manager (offers to install it if missing)
#   2. uv sync     — create the venv and install all dependencies
#   3. setup.py    — scaffold .chester/ (config, workspace identity, skills)
#   4. QGIS        — locate qgis_process (warns, never fails — QGIS is optional to install)
#   5. LLM         — pick a provider (Ollama / Anthropic / OpenAI / Google) and
#                    wire it up: patch the model string in the config, and for
#                    hosted providers store the API key in a local .env (loaded
#                    by gateway.py / ask.py). For Ollama it checks reachability
#                    and offers to pull the model.
#
# Everything is idempotent — re-running is safe. Interactive by default; pass
# --yes for a non-interactive install (keeps the Ollama default, auto-confirms).
#
# Usage:
#   ./install.sh                       # interactive
#   ./install.sh --yes                 # non-interactive (Ollama default)
#   ./install.sh --provider anthropic  # preselect a provider (prompts for model/key)
#   ./install.sh --provider anthropic --model claude-sonnet-5 --api-key sk-…
#   ./install.sh --provider ollama --model gemma4:26b
#   ./install.sh --no-pull             # skip the Ollama model pull
#
set -euo pipefail

cd "$(dirname "$0")"

# ── Pretty output ───────────────────────────────────────────────────────────
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
  YLW=$'\033[33m'; BLU=$'\033[34m'; RST=$'\033[0m'
else
  BOLD=; DIM=; RED=; GRN=; YLW=; BLU=; RST=
fi
step() { echo; echo "${BOLD}${BLU}▸ $*${RST}"; }
ok()   { echo "  ${GRN}✔${RST} $*"; }
warn() { echo "  ${YLW}⚠${RST} $*"; }
err()  { echo "  ${RED}✗${RST} $*" >&2; }
info() { echo "  ${DIM}$*${RST}"; }

# ── Argument parsing ────────────────────────────────────────────────────────
ASSUME_YES=false
PROVIDER=""
MODEL=""
API_KEY=""
DO_PULL=true

# Print the leading comment block (from line 3 until the first non-comment line).
usage() { awk 'NR>2 && /^#/ {sub(/^# ?/,""); print; next} NR>2 {exit}' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)      ASSUME_YES=true ;;
    --provider)    PROVIDER="${2:-}"; shift ;;
    --provider=*)  PROVIDER="${1#*=}" ;;
    --model)       MODEL="${2:-}"; shift ;;
    --model=*)     MODEL="${1#*=}" ;;
    --api-key)     API_KEY="${2:-}"; shift ;;
    --api-key=*)   API_KEY="${1#*=}" ;;
    --no-pull)     DO_PULL=false ;;
    -h|--help)     usage 0 ;;
    *) err "Unbekannte Option: $1"; usage 2 ;;
  esac
  shift
done

# A prompt only makes sense on a TTY and when the user did not pass --yes.
interactive() { [ "$ASSUME_YES" = false ] && [ -t 0 ]; }

# ask "Question?" default  → echoes the answer (default on empty / non-interactive)
ask() {
  local q="$1" default="${2:-}" reply
  if ! interactive; then echo "$default"; return; fi
  read -r -p "  ${BOLD}${q}${RST} ${DIM}[${default}]${RST} " reply || true
  echo "${reply:-$default}"
}

# confirm "Question?" → returns 0 for yes (default yes; auto-yes with --yes)
confirm() {
  local reply
  if ! interactive; then return 0; fi
  read -r -p "  ${BOLD}$1${RST} ${DIM}[Y/n]${RST} " reply || true
  case "${reply:-y}" in [nN]*) return 1 ;; *) return 0 ;; esac
}

echo "${BOLD}🌍 Chester — Installation${RST}"
info "Arbeitsverzeichnis: $(pwd)"

# ── 1. uv ───────────────────────────────────────────────────────────────────
step "1/5  uv (Python-Paketmanager)"
if command -v uv >/dev/null 2>&1; then
  ok "uv gefunden: $(uv --version)"
else
  warn "uv ist nicht installiert."
  if confirm "uv jetzt via offiziellem Installer holen?"; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer drops uv into ~/.local/bin (or ~/.cargo/bin); make it visible now.
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    command -v uv >/dev/null 2>&1 || { err "uv weiterhin nicht im PATH — bitte Shell neu starten und install.sh erneut ausführen."; exit 1; }
    ok "uv installiert: $(uv --version)"
  else
    err "Ohne uv geht es nicht. Siehe https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  fi
fi

# ── 2. Dependencies ─────────────────────────────────────────────────────────
step "2/5  Abhängigkeiten installieren (uv sync)"
uv sync
ok "Virtuelle Umgebung und Pakete sind bereit."

# ── 3. Scaffold .chester/ ───────────────────────────────────────────────────
step "3/5  Projekt einrichten (Config, Identität, Skills)"
uv run python setup.py
ok "Scaffolding abgeschlossen (.chester/)."

# ── 4. QGIS ─────────────────────────────────────────────────────────────────
step "4/5  QGIS (qgis_process) suchen"
if uv run python -m chester.qgis_env >/tmp/chester_qgis.$$ 2>&1; then
  ok "QGIS gefunden:"
  sed 's/^/    /' /tmp/chester_qgis.$$
else
  warn "qgis_process nicht gefunden — die GIS-Werkzeuge brauchen es."
  info "Installiere QGIS (LTR) oder setze CHESTER_QGIS_PROCESS_BIN / CHESTER_QGIS_APP."
  info "Der Agent startet trotzdem; GIS-Operationen schlagen ohne QGIS fehl."
fi
rm -f /tmp/chester_qgis.$$

# ── 5. LLM provider ─────────────────────────────────────────────────────────
step "5/5  LLM-Provider konfigurieren"

# Provider auswählen (Menü, sofern nicht per Flag vorgegeben).
if [ -z "$PROVIDER" ]; then
  if interactive; then
    echo "  Welchen Provider möchtest du nutzen?"
    echo "    ${BOLD}1${RST}) Ollama    ${DIM}— lokal, kein API-Key (Standard)${RST}"
    echo "    ${BOLD}2${RST}) Anthropic ${DIM}— Claude (ANTHROPIC_API_KEY)${RST}"
    echo "    ${BOLD}3${RST}) OpenAI    ${DIM}— GPT (OPENAI_API_KEY)${RST}"
    echo "    ${BOLD}4${RST}) Google    ${DIM}— Gemini (GOOGLE_API_KEY)${RST}"
    case "$(ask 'Auswahl 1-4:' 1)" in
      2) PROVIDER=anthropic ;;
      3) PROVIDER=openai ;;
      4) PROVIDER=google ;;
      *) PROVIDER=ollama ;;
    esac
  else
    PROVIDER=ollama
  fi
fi

# Per-provider defaults for the model name and the env var that holds the key.
case "$PROVIDER" in
  ollama)    DEFAULT_MODEL="gemma4:26b"; KEY_VAR="" ;;
  anthropic) DEFAULT_MODEL="claude-sonnet-5";    KEY_VAR="ANTHROPIC_API_KEY" ;;
  openai)    DEFAULT_MODEL="gpt-5.6-terra";      KEY_VAR="OPENAI_API_KEY" ;;
  google)    DEFAULT_MODEL="gemini-3.6-flash";   KEY_VAR="GOOGLE_API_KEY" ;;
  *) err "Unbekannter Provider: $PROVIDER (erlaubt: ollama, anthropic, openai, google)"; exit 2 ;;
esac
[ -n "$MODEL" ] || MODEL="$(ask "Modellname für ${PROVIDER}:" "$DEFAULT_MODEL")"
MODEL_STRING="${PROVIDER}/${MODEL}"

# Write .env KEY=VALUE, replacing any existing line for that key.
write_env() {
  local key="$1" val="$2"
  touch .env
  # Drop an existing line for this key, then append the new one.
  if grep -q "^${key}=" .env 2>/dev/null; then
    grep -v "^${key}=" .env > .env.tmp && mv .env.tmp .env
  fi
  printf '%s=%s\n' "$key" "$val" >> .env
  chmod 600 .env
}

# Patch model.model (and base_url for Ollama) in .chester/chester.json.
uv run python - "$MODEL_STRING" <<'PY'
import json, sys
from pathlib import Path
p = Path(".chester/chester.json")
cfg = json.loads(p.read_text(encoding="utf-8"))
cfg.setdefault("model", {})["model"] = sys.argv[1]
if sys.argv[1].startswith("ollama/"):
    cfg["model"].setdefault("base_url", "http://localhost:11434/v1")
p.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
print(f"  Konfiguriert: model.model = {sys.argv[1]}")
PY
ok "Modell in .chester/chester.json gesetzt: ${BOLD}${MODEL_STRING}${RST}"

if [ "$PROVIDER" = "ollama" ]; then
  # Reachability + model presence for the local backend.
  OLLAMA_ROOT="$(uv run python -c 'from selmakit.config import load_config; from agent_build import CONFIG_NAME, STATE_DIR; print(load_config(STATE_DIR, config_name=CONFIG_NAME).model.effective_base_url.rstrip("/").removesuffix("/v1"))')"
  if curl -sf -o /dev/null --max-time 3 "${OLLAMA_ROOT}/api/tags"; then
    ok "Ollama erreichbar unter ${OLLAMA_ROOT}."
    if curl -sf --max-time 3 "${OLLAMA_ROOT}/api/tags" | grep -q "\"${MODEL}\""; then
      ok "Modell '${MODEL}' ist bereits vorhanden."
    elif [ "$DO_PULL" = true ] && confirm "Modell '${MODEL}' jetzt per 'ollama pull' laden?"; then
      if command -v ollama >/dev/null 2>&1; then
        ollama pull "$MODEL" && ok "Modell geladen."
      else
        warn "'ollama' CLI nicht gefunden — bitte manuell: ollama pull ${MODEL}"
      fi
    else
      info "Später laden mit: ollama pull ${MODEL}"
    fi
  else
    warn "Ollama unter ${OLLAMA_ROOT} nicht erreichbar. Starte es mit 'ollama serve'."
    info "Danach: ollama pull ${MODEL}"
  fi
else
  # Hosted provider — capture the API key into .env (loaded by gateway.py/ask.py).
  if [ -z "$API_KEY" ] && interactive; then
    read -r -s -p "  ${BOLD}${KEY_VAR} (Eingabe verborgen, leer = überspringen):${RST} " API_KEY || true
    echo
  fi
  if [ -n "$API_KEY" ]; then
    write_env "$KEY_VAR" "$API_KEY"
    ok "${KEY_VAR} in .env gespeichert (chmod 600, gitignored)."
  else
    warn "Kein ${KEY_VAR} gesetzt — trage ihn vor dem Start in .env ein:"
    info "echo '${KEY_VAR}=dein-key' >> .env"
  fi
fi

# ── Done ────────────────────────────────────────────────────────────────────
echo
echo "${BOLD}${GRN}✔ Installation abgeschlossen.${RST}"
echo
echo "  ${BOLD}Starten:${RST}"
echo "    ./start.sh                 ${DIM}# Gateway (:8000) + Dashboard (:8501)${RST}"
echo "    uv run ask.py \"…\"          ${DIM}# CLI-Chat, ohne Web-Stack${RST}"
echo
echo "  ${BOLD}Nützlich:${RST}"
echo "    uv run data.py             ${DIM}# GeoCache-Inventar ansehen${RST}"
echo "    uv run trace.py last --full ${DIM}# letzten Lauf inspizieren${RST}"
