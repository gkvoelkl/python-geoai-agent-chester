"""Chester — Test-Level 4: mehrstufige Dialoge gegen den laufenden Agenten.

Ein Dialog ist **eine** Sitzung: Die Schritte laufen nacheinander unter demselben
Sitzungsschlüssel, damit Gedächtnis, Bezug und Aufräumen überhaupt geprüft werden
können. Genau das kann `testprompt.py` nicht — es löscht die Sitzung vor jedem Lauf,
weil Wiederholungen vergleichbar bleiben müssen.

    uv run dialog.py                # alle Dialoge
    uv run dialog.py <id>           # einen, mit vollem Werkzeug-Protokoll
    uv run dialog.py --list

Bewertet wird zweigeteilt (`doc/test-levels.md`): Die **maschinellen** Prüfungen aus
`chester/dialogs.py` entscheiden über bestanden/durchgefallen; die **Prosa-Kriterien**
werden unbewertet ausgegeben und archiviert — ein Urteil, das niemand gefällt hat, ist
schlechter als ein offen gelassenes.

Die Schritte stehen **vor** dem Lauf fest (`agent-dialog-tests.jsonl`). Das ist die Regel,
die den Aufbau ehrlich hält: Wer den Nutzer im Moment spielt, redet sich das Ergebnis
schön (`doc/agent-test-dialogs.md`, „Die Regel, die es sauber hält").
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from selmakit import Gateway

from agent_build import (
    CONFIG_NAME,
    STATE_DIR,
    geo_capabilities,
    register_validation_gate,
    selmakit_capabilities,
)
from ask import ask
from chester.dialogs import Turn, append_history, evaluate
from setup import setup
from testprompt import clear_session, config_model_name, validation_note

DIALOGS = Path(__file__).parent / "agent-dialog-tests.jsonl"
#: Zeitdeckel je **Schritt**. Großzügiger als bei den Proben: Ein Dialogschritt ist eine
#: ganze Aufgabe, kein Einzelschritt.
DEFAULT_TIMEOUT_S = 900


def load_dialogs() -> list[dict]:
    lines = DIALOGS.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def save_dialogs(dialogs: list[dict]) -> None:
    """Die Dialogdatei schreiben (die Bench-UI bearbeitet sie)."""
    DIALOGS.write_text(
        "\n".join(json.dumps(d, ensure_ascii=False) for d in dialogs) + "\n", encoding="utf-8"
    )


def workspace() -> Path:
    from chester.workspace import DEFAULT_WORKSPACE, resolve_path

    return Path(resolve_path("x.gpkg", DEFAULT_WORKSPACE)).parent


def _snapshot(ws: Path) -> set[str]:
    return {str(p) for p in ws.glob("*") if p.is_file()}


async def run_turn(agent, session_key: str, spec: dict, ws: Path, timeout_s: float, sink) -> Turn:
    """Einen Schritt fahren und festhalten, was er getan und hinterlassen hat."""
    turn = Turn(spec["prompt_de"])
    before = _snapshot(ws)

    def on_event(kind: str, fields: dict) -> None:
        if kind == "tool_call":
            turn.tool_calls.append((fields.get("name", "?"), fields.get("args")))
        elif kind == "tool_result":
            turn.tool_results.append(fields.get("result"))

    started = time.monotonic()
    try:
        answer = await asyncio.wait_for(
            ask(agent, turn.prompt, session_key=session_key,
                show_tools=True, sink=sink, on_event=on_event),
            timeout=timeout_s,
        )
    except TimeoutError:
        answer = None
        turn.timed_out = True
    turn.duration_s = time.monotonic() - started
    turn.answer = answer or ""
    # Was dieser Schritt geschrieben hat — die Grundlage für „ist das Ergebnis leer?".
    turn.written = sorted(_snapshot(ws) - before)
    return turn


async def run_dialog(agent, dialog: dict, ws: Path, timeout_s: float, verbose: bool):
    """Alle Schritte eines Dialogs in **einer** Sitzung."""
    session_key = f"dialog:{dialog['id']}"
    clear_session(session_key)  # ein Dialog beginnt am Anfang, nicht in der Mitte
    sink = (lambda s: print(s, end="", flush=True)) if verbose else (lambda s: None)

    turns: list[Turn] = []
    for i, spec in enumerate(dialog["turns"], 1):
        print(f"\n  ── Schritt {i}/{len(dialog['turns'])} ─────────────────────────────")
        print(f"  » {spec['prompt_de']}")
        turn = await run_turn(agent, session_key, spec, ws, timeout_s, sink)
        note = validation_note(turn.answer)
        print(f"  ← {len(turn.tool_calls)} Aufrufe, {turn.duration_s:.0f}s"
              + (f" · [gate] {note[:80]}" if note else ""))
        turns.append(turn)
        if turn.timed_out:
            # Abgebrochen heißt: keine Sitzung geschrieben. Die folgenden Schritte
            # begännen bei null und prüften etwas anderes als den Dialog.
            print(f"  ⛔ Deckel gerissen — Dialog hier beendet, {len(dialog['turns']) - i} "
                  "Schritt(e) entfallen")
            break
    return turns


def archive(dialog: dict, turns: list[Turn], *, passed: bool, lines: list[str]) -> None:
    """Einen Dialoglauf festhalten — dieselbe Zeilenform für CLI und Bench."""
    append_history({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "id": dialog["id"],
        "category": dialog.get("category", ""),
        "model": config_model_name(),
        "passed": bool(passed),
        "checks": lines,
        "turns": [
            {"prompt": t.prompt, "tools": t.tools, "duration_s": round(t.duration_s, 1),
             "written": [Path(p).name for p in t.written], "answer": t.answer}
            for t in turns
        ],
    })


async def run_all(dialogs: list[dict], verbose: bool, timeout_s: float) -> int:
    setup(quiet=True)
    load_dotenv()
    agent = Gateway.from_config(
        STATE_DIR, CONFIG_NAME,
        capabilities=selmakit_capabilities,
        extra_capabilities=geo_capabilities(),
    ).agent
    register_validation_gate(agent)  # dieselbe Verdrahtung wie im Produkt
    ws = workspace()

    print(f"Test-Level 4 — {len(dialogs)} Dialog(e), Modell {config_model_name()}")
    passed_n = 0
    for i, dialog in enumerate(dialogs, 1):
        print(f"\n===== [{i}/{len(dialogs)}] {dialog['id']} · {dialog['category']} =====")
        turns = await run_dialog(agent, dialog, ws, timeout_s, verbose)
        passed, lines = evaluate(dialog, turns, workspace=ws)
        passed_n += passed
        print(f"\n  {'PASS' if passed else 'FAIL'} — maschinelle Prüfungen")
        for line in lines:
            print(line)
        if dialog.get("judge_criteria"):
            print("  offen (Auslegung, nicht bewertet):")
            for c in dialog["judge_criteria"]:
                print(f"    · {c}")
        archive(dialog, turns, passed=passed, lines=lines)
    print(f"\n{passed_n}/{len(dialogs)} bestanden (maschinelle Prüfungen)")
    return 0 if passed_n == len(dialogs) else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Test-Level 4 — Dialogtests")
    ap.add_argument("dialog_id", nargs="?", help="nur diesen Dialog fahren")
    ap.add_argument("--verbose", action="store_true", help="Werkzeug-Austausch mitschreiben")
    ap.add_argument("--list", action="store_true", help="Dialoge auflisten, nichts fahren")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"Zeitdeckel je Schritt in Sekunden (Vorgabe {DEFAULT_TIMEOUT_S})")
    args = ap.parse_args()

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)  # ein Lauf dauert Minuten

    dialogs = load_dialogs()
    if args.list:
        for d in dialogs:
            print(f"{d['id']:24s} {d['category']:26s} {len(d['turns'])} Schritte · "
                  f"{len(d.get('checks', []))} Prüfungen")
        return
    if args.dialog_id:
        dialogs = [d for d in dialogs if d["id"] == args.dialog_id]
        if not dialogs:
            print(f"unbekannter Dialog: {args.dialog_id}", file=sys.stderr)
            sys.exit(2)
    sys.exit(asyncio.run(run_all(dialogs, args.verbose or bool(args.dialog_id), args.timeout)))


if __name__ == "__main__":
    main()
