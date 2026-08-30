"""Chester — Test-Level 2: Mikro-Geo-Tasks gegen den laufenden Agenten.

Eine Aufgabe, ein Werkzeug, ein exakter Sollwert — gemessen am **erzeugten Artefakt**
und an den **Rückgabewerten der Werkzeuge**, nie am Antworttext. Kein Judge, kein Netz.
Zweck ist ein Vorfilter: ob ein anderes Modell überhaupt in Frage kommt, muss man in
Minuten beantworten können. Die Systematik steht in `doc/test-levels.md`.

    uv run probe.py                 # alle Proben, dann k/n
    uv run probe.py <id>            # eine einzelne, mit Werkzeug-Protokoll
    uv run probe.py --verbose       # alle, jede mit Protokoll

**Warum alle Proben in einem Prozess laufen:** Der System-Prompt bleibt über alle
Aufgaben gleich, also wird die kalte Prefill genau einmal bezahlt (gemessen 78,6 s
kalt gegen 0,1 s im Cache). Ein Runner, der je Aufgabe einen Prozess startet, macht
den Vorfilter kaputt.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import shutil
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
from chester.probes import append_history, effective_timeout, evaluate
from setup import setup
from testprompt import clear_session, config_model_name

#: Zeitdeckel je Probe. Eine Ein-Operations-Aufgabe, die ihn reißt, ist gescheitert —
#: egal, was sie danach noch versucht. Ohne Deckel bestimmt der schlechteste Fall die
#: Laufzeit des ganzen Vorfilters: `join-leading-zero-ags` kreiste am 2026-08-29
#: **elf Stunden** über 82 Werkzeugaufrufe (56× `qgis_python`) und lieferte am Ende
#: eine leere Ebene.
DEFAULT_TIMEOUT_S = 180

TASKS = Path(__file__).parent / "agent-probe-tasks.jsonl"
FIXTURES = Path(__file__).parent / "samples" / "probe"


def workspace() -> Path:
    """Das Verzeichnis, in dem Ausgaben landen — dasselbe wie für jeden Lauf."""
    from chester.workspace import DEFAULT_WORKSPACE, resolve_path

    return Path(resolve_path("x.gpkg", DEFAULT_WORKSPACE)).parent


def load_tasks() -> list[dict]:
    lines = TASKS.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def stage_fixtures(ws: Path, task: dict) -> None:
    """Die Fixtures der Aufgabe in den Arbeitsbereich legen (immer frisch).

    Die Fixtures liegen eingecheckt in `samples/probe/`; fehlt eine, erzeugt
    `samples/make_probe_fixtures.py` den ganzen Satz neu und rechnet die Sollwerte
    dabei vor.
    """
    ws.mkdir(parents=True, exist_ok=True)
    for name in task.get("fixtures", []):
        src = FIXTURES / name
        if not src.is_file():
            raise SystemExit(
                f"Fixture fehlt: {src}\n"
                "Einmal erzeugen:  uv run python samples/make_probe_fixtures.py"
            )
        shutil.copyfile(src, ws / name)


def clear_outputs(ws: Path, task: dict) -> None:
    """Alles entfernen, was diese Aufgabe erzeugen soll — sonst besteht ein Lauf
    auf der Ausgabe des vorigen (genau der Stale-State-Fall aus den Dialogtests)."""
    globs = [a["path"] for a in task["assertions"] if "path" in a]
    globs += [a["glob"] for a in task["assertions"] if "glob" in a]
    for pattern in globs:
        for hit in ws.glob(pattern):
            hit.unlink(missing_ok=True)


async def run_task(  # noqa: PLR0913  # ein Lauf hat Kontext, Aufgabe, Ort, Deckel, Ausgabe
    agent, task: dict, ws: Path, verbose: bool, timeout_s: float, sink=None
) -> tuple[bool, float, list[str]]:
    """Eine Probe fahren und auswerten."""
    tool_results: list = []

    def on_event(kind: str, fields: dict) -> None:
        if kind == "tool_result":
            tool_results.append(fields.get("result"))

    session_key = f"probe:{task['id']}"
    clear_session(session_key)
    stage_fixtures(ws, task)
    clear_outputs(ws, task)

    timeout_s = effective_timeout(task, timeout_s)
    started = time.monotonic()
    if sink is None:
        sink = (lambda s: print(s, end="", flush=True)) if verbose else (lambda s: None)
    timed_out = False
    try:
        # `on_event` ist die einzige Quelle für die Werkzeug-Rückgaben: `value_seen`
        # prüft gegen sie, nicht gegen den Antworttext.
        await asyncio.wait_for(
            ask(
                agent, task["prompt_de"], session_key=session_key,
                show_tools=True, sink=sink, on_event=on_event,
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        timed_out = True
    duration = time.monotonic() - started

    passed, lines = evaluate(task, workspace=ws, tool_results=tool_results)
    archive(task, passed=passed and not timed_out, duration_s=duration,
            timed_out=timed_out, lines=lines)
    if timed_out:
        # Die Prüfungen laufen trotzdem: Was bis dahin geschrieben wurde, ist die
        # ehrlichere Auskunft als ein blankes "abgebrochen".
        lines.insert(0, f"  ✗ Zeitdeckel: nach {timeout_s:.0f}s abgebrochen")
        passed = False
    return passed, duration, lines


def archive(task: dict, *, passed: bool, duration_s: float, timed_out: bool,
            lines: list[str]) -> None:
    """Eine Zeile in die Proben-Historie — dieselbe Rolle wie `history.jsonl` für die Bank."""
    append_history({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "id": task["id"],
        "operation": task.get("operation", ""),
        "model": config_model_name(),
        "passed": bool(passed),
        "timed_out": bool(timed_out),
        "duration_s": round(duration_s, 1),
        "checks": lines,
    })


def save_tasks(tasks: list[dict]) -> None:
    """Die Aufgabendatei schreiben (die Bench-UI bearbeitet sie)."""
    TASKS.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n", encoding="utf-8"
    )


async def run_all(tasks: list[dict], verbose: bool, timeout_s: float) -> int:
    setup(quiet=True)
    load_dotenv()
    agent = Gateway.from_config(
        STATE_DIR,
        CONFIG_NAME,
        capabilities=selmakit_capabilities,
        extra_capabilities=geo_capabilities(),
    ).agent
    register_validation_gate(agent)  # dieselbe Verdrahtung wie im Produkt
    ws = workspace()

    print(f"Test-Level 2 — {len(tasks)} Proben, Modell {config_model_name()}")
    # Einmal warmlaufen, ausserhalb der Messung: Die kalte Prefill kostet auf dieser
    # Maschine ~160 s (gegen 0,1 s im Cache) und wuerde sonst die erste Probe gegen
    # den Zeitdeckel druecken — gemessen wuerde dann der Cache, nicht das Modell.
    warm = time.monotonic()
    await ask(agent, "Antworte nur mit: bereit.", session_key="probe:warmup",
              show_tools=False, sink=lambda s: None)
    print(f"(Warmlauf {time.monotonic() - warm:.0f}s — die kalte Prefill zaehlt nicht mit)\n")
    passed_n = 0
    for i, task in enumerate(tasks, 1):
        if verbose:
            print(f"\n===== [{i}/{len(tasks)}] {task['id']} =====")
            print(f"Falle: {task['trap']}\n")
        ok, duration, lines = await run_task(agent, task, ws, verbose, timeout_s)
        passed_n += ok
        mark = "PASS" if ok else "FAIL"
        print(f"[{i}/{len(tasks)}] {task['id']:28s} {mark}  {duration:5.0f}s"
              f"  ({task['operation']})")
        for line in lines:
            if not ok or verbose:
                print(line)
    print(f"\n{passed_n}/{len(tasks)} bestanden")
    return 0 if passed_n == len(tasks) else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Test-Level 2 — Mikro-Geo-Tasks")
    ap.add_argument("task_id", nargs="?", help="nur diese Probe fahren")
    ap.add_argument("--verbose", action="store_true", help="Werkzeug-Austausch mitschreiben")
    ap.add_argument("--list", action="store_true", help="Proben auflisten, nichts fahren")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"Zeitdeckel je Probe in Sekunden (Vorgabe {DEFAULT_TIMEOUT_S})")
    args = ap.parse_args()

    # Ungepuffert schreiben: ein Durchlauf dauert Minuten, und in eine Datei
    # umgeleitet erschien sonst bis zum Schluss keine einzige Zeile — der erste
    # Hintergrundlauf sah zehn Minuten lang aus wie ein Hänger.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

    tasks = load_tasks()
    if args.list:
        for t in tasks:
            print(f"{t['id']:28s} {t['operation']:16s} {t['trap']}")
        return
    if args.task_id:
        tasks = [t for t in tasks if t["id"] == args.task_id]
        if not tasks:
            print(f"unbekannte Probe: {args.task_id}", file=sys.stderr)
            sys.exit(2)
    # Fehlende Fixtures **vor** dem Agentenbau melden. Dieselbe Regel wie beim Judge
    # in `testprompt.py`: Was den Lauf ohnehin scheitern lässt, gehört vor den teuren
    # Teil — gemessen kostete die späte Meldung Modellstart und Warmlauf.
    missing = sorted({f for t in tasks for f in t.get("fixtures", [])
                      if not (FIXTURES / f).is_file()})
    if missing:
        print(
            f"Fixtures fehlen ({', '.join(missing)}).\n"
            "Neu erzeugen:  uv run python samples/make_probe_fixtures.py",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(asyncio.run(run_all(tasks, args.verbose or bool(args.task_id), args.timeout)))


if __name__ == "__main__":
    main()
