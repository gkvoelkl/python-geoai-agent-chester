"""Chester — Test-Level 2: micro geo tasks against the running agent.

One task, one tool, one exact expected value — measured on the **produced artifact**
and the **tools' return values**, never the answer text. No judge, no network. The
purpose is a pre-filter: whether another model is worth considering at all must be
answerable in minutes. The method is in `doc/test-levels.md`.

    uv run probe.py                 # all probes, then k/n
    uv run probe.py <id>            # a single one, with the tool protocol
    uv run probe.py --verbose       # all, each with its protocol

**Why all probes run in one process:** the system prompt is the same for every task, so
the cold prefill is paid exactly once (measured 78.6 s cold against 0.1 s cached). A
runner that starts a process per task breaks the pre-filter.
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

from agents import build_agent
from ask import ask
from chester import toolchoice
from chester.evalcells import agent_kind
from chester.probes import (
    append_history,
    effective_timeout,
    evaluate,
    read_history,
    timeout_decides,
)
from chester.qgis_env import qgis_available
from setup import setup
from testprompt import clear_session, config_model_name

#: Time limit per probe. A one-operation task that breaks it has failed — whatever it
#: tries afterwards. Without a limit the worst case sets the runtime of the whole
#: pre-filter: `join-leading-zero-ags` circled for **eleven hours** on 2026-08-29 over 82
#: tool calls (56× `qgis_python`) and delivered an empty layer in the end.
#:
#: **180 → 320 s on 2026-09-01.** The first complete pass showed that 180 s measure the
#: budget, not the task: all three failures broke the limit, none gave a wrong number.
#: `height-gini` got to **one** call — a finished snippet, refused by the search-first
#: gate, and the second round no longer fit. A limit that rules out a correction round
#: measures reaction time instead of the geo decision.
#:
#: **320 → 480 s on 2026-09-01.** The same finding one step later: in the 14:25 pass
#: **six of eleven** probes broke the limit, two of them passed anyway (`area-in-degrees`,
#: `union-not-sum`) — the artifact was right, the agent just never finished. A limit
#: half the field breaks no longer separates "cannot" from "was not done".
DEFAULT_TIMEOUT_S = 480
#: The team runs an orchestrator plus one agent run per ressort — its first real run
#: needed ~3 min for a one-step task (2026-09-19). Its own default, said out loud at
#: start, so a longer cap never shifts a comparison unnoticed.
TEAM_TIMEOUT_S = 900

TASKS = Path(__file__).parent / "probes" / "agent-probe-tasks.jsonl"
FIXTURES = Path(__file__).parent / "probes" / "fixtures"


def workspace() -> Path:
    """The directory outputs land in — the same as for every run."""
    from chester.workspace import DEFAULT_WORKSPACE, resolve_path

    return Path(resolve_path("x.gpkg", DEFAULT_WORKSPACE)).parent


def load_tasks() -> list[dict]:
    lines = TASKS.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def stage_fixtures(ws: Path, task: dict) -> None:
    """Put the task's fixtures into the workspace (always fresh).

    The fixtures are checked in under `probes/fixtures/`; if one is missing,
    `probes/make_fixtures.py` regenerates the whole set and computes the expected
    values along the way.
    """
    ws.mkdir(parents=True, exist_ok=True)
    for name in task.get("fixtures", []):
        src = FIXTURES / name
        if not src.is_file():
            raise SystemExit(
                f"Fixture fehlt: {src}\n"
                "Einmal erzeugen:  uv run python probes/make_fixtures.py"
            )
        shutil.copyfile(src, ws / name)


def with_fixture_note(task: dict) -> str:
    """The task text plus one line saying where the inputs are.

    Measured 2026-09-01, first complete pass: **37 of 71** calls were file searches (22×
    `list_directory`, 15× `find_files`). The three fast probes did not search at all —
    they called `check_crs("green.gpkg")` and `resolve_path` found the file at once. So
    the route holds; the agent just stops trusting it once a `find_files("*.gpkg")`
    answers "No matches found" (it does not search recursively) and
    `list_directory(".")` shows a second, almost empty `geocache/`.

    Test-Level 2 measures the **geo decision**, not the ability to find a file. Where
    the input is is therefore part of the task statement, not of the check — the same
    separation as the warm-up, which stands outside the measurement.
    """
    names = task.get("fixtures") or []
    if not names:
        return str(task["prompt_de"])
    listed = ", ".join(names)
    return (f"{task['prompt_de']}\n\n"
            f"Die Eingabedatei(en) liegen im GeoCache: {listed}. "
            f"Gib sie den Werkzeugen genau unter diesem Namen an — sie werden dort "
            f"gefunden; ein Suchen im Dateisystem ist nicht nötig.")


def clear_outputs(ws: Path, task: dict) -> None:
    """Remove everything this task is to produce — otherwise a run passes on the previous
    run's output (exactly the stale-state case from the dialogue tests)."""
    globs = [a["path"] for a in task["assertions"] if "path" in a]
    globs += [a["glob"] for a in task["assertions"] if "glob" in a]
    for pattern in globs:
        for hit in ws.glob(pattern):
            hit.unlink(missing_ok=True)


async def run_task(  # noqa: PLR0913  # ein Lauf hat Kontext, Aufgabe, Ort, Deckel, Ausgabe
    agent, task: dict, ws: Path, verbose: bool, timeout_s: float, sink=None
) -> tuple[bool, float, list[str]]:
    """Run one probe and evaluate it."""
    tool_results: list = []
    called: list[str] = []  # tool names, for the tool-choice figure (never graded)

    def on_event(kind: str, fields: dict) -> None:
        if kind == "tool_result":
            tool_results.append(fields.get("result"))
        elif kind == "tool_call":
            called.append(str(fields.get("name")))

    session_key = f"probe:{task['id']}"
    clear_session(session_key)
    stage_fixtures(ws, task)
    clear_outputs(ws, task)
    prompt = with_fixture_note(task)

    timeout_s = effective_timeout(task, timeout_s)
    started = time.monotonic()
    if sink is None:
        sink = (lambda s: print(s, end="", flush=True)) if verbose else (lambda s: None)
    timed_out = False
    try:
        # `on_event` is the only source of the tool returns: `value_seen` checks against
        # them, not against the answer text.
        await asyncio.wait_for(
            ask(
                agent, prompt, session_key=session_key,
                show_tools=True, sink=sink, on_event=on_event,
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        timed_out = True
    duration = time.monotonic() - started

    passed, lines = evaluate(task, workspace=ws, tool_results=tool_results)
    if timed_out:
        # The checks run anyway: what was written by then is a more honest answer than
        # a bare "aborted". Whether the overrun flips the verdict is the probe's call
        # (`requires_finish`), not the runner's — for a computation the artifact is the
        # answer.
        decides = timeout_decides(task)
        mark = "✗" if decides else "⏱"
        lines.insert(0, f"  {mark} Zeitdeckel: nach {timeout_s:.0f}s abgebrochen"
                        + ("" if decides else " (Prüfungen zählen trotzdem)"))
        passed = passed and not decides
    used = toolchoice.tools_used(called, tool_results)  # inside the ressorts, for the team
    lines.append(toolchoice.describe(task, used, qgis=qgis_available()))
    hit = toolchoice.ressort_hit(task, called)
    if hit is not None:
        mark = "✓" if hit else "✗"
        lines.append(f"  {mark} ressort choice (not graded): expected "
                     f"ressort_{task.get('expected_ressort')} — called "
                     f"{', '.join(dict.fromkeys(c for c in called if c.startswith('ressort_')))}")
    archive(task, passed=passed, duration_s=duration, timed_out=timed_out, lines=lines,
            called=called, used=used)
    return passed, duration, lines


def archive(  # noqa: PLR0913  # one history row carries the run and its tool choice
    task: dict, *, passed: bool, duration_s: float, timed_out: bool,
    lines: list[str], called: list[str] | None = None, used: list[str] | None = None,
) -> None:
    """One line into the probe history — the same role as `history.jsonl` for the bank."""
    append_history({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "id": task["id"],
        "operation": task.get("operation", ""),
        "model": config_model_name(),
        "agent": agent_kind(),  # agent | team — never averaged together
        "passed": bool(passed),
        "timed_out": bool(timed_out),
        "duration_s": round(duration_s, 1),
        "checks": lines,
        # The tool-choice baseline for chester-team (chester.toolchoice).
        "tools_called": called or [],
        # For the team, the tools inside the ressorts; for the agent, the same list.
        "tools_used": used if used is not None else called or [],
        "tool_hit": toolchoice.tool_hit(task, used if used is not None else called or [],
                                        qgis=qgis_available()),
        "ressort_hit": toolchoice.ressort_hit(task, called or []),
        "expected_ressort": task.get("expected_ressort"),
    })


def save_tasks(tasks: list[dict]) -> None:
    """Die Aufgabendatei schreiben (die Bench-UI bearbeitet sie)."""
    TASKS.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n", encoding="utf-8"
    )


async def run_all(tasks: list[dict], verbose: bool, timeout_s: float) -> int:
    setup(quiet=True)
    load_dotenv()
    agent = build_agent()
    ws = workspace()

    print(f"Test-Level 2 — {len(tasks)} Proben, Modell {config_model_name()}")
    # Warm up once, outside the measurement: the cold prefill costs ~160 s on this machine
    # (against 0.1 s cached) and would otherwise push the first probe into its time
    # limit — what got measured would be the cache, not the model.
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
    hits, measured = toolchoice.hit_rate(read_history(limit=len(tasks)))
    if measured:
        print(f"Werkzeugwahl: {hits}/{measured} Proben mit erwartetem Werkzeug "
              "(zählt nicht fürs Bestehen)")
    r_hits, r_measured = toolchoice.hit_rate(read_history(limit=len(tasks)), "ressort_hit")
    if r_measured:
        print(f"Ressortwahl: {r_hits}/{r_measured} Proben ans erwartete Ressort")
    return 0 if passed_n == len(tasks) else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Test-Level 2 — Mikro-Geo-Tasks")
    # Several names allowed: a pass builds the agent **once** and warms the model up
    # **once**. Starting eight probes one by one cost eight cold starts, and the warm-up
    # stands outside the measurement on purpose.
    ap.add_argument("task_id", nargs="*", help="nur diese Probe(n) fahren")
    ap.add_argument("--verbose", action="store_true", help="Werkzeug-Austausch mitschreiben")
    ap.add_argument("--list", action="store_true", help="Proben auflisten, nichts fahren")
    ap.add_argument("--timeout", type=float, default=None,
                    help=f"Zeitdeckel je Probe in Sekunden (Vorgabe {DEFAULT_TIMEOUT_S}, "
                         f"Team {TEAM_TIMEOUT_S})")
    args = ap.parse_args()

    # Write unbuffered: a pass takes minutes, and redirected into a file not a single
    # line appeared until the end otherwise — the first background run looked like a
    # hang for ten minutes.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)

    tasks = load_tasks()
    if args.list:
        for t in tasks:
            print(f"{t['id']:28s} {t['operation']:16s} {t['trap']}")
        return
    if args.task_id:
        wanted = list(args.task_id)
        unknown = [w for w in wanted if not any(t["id"] == w for t in tasks)]
        if unknown:
            print(f"unbekannte Probe(n): {', '.join(unknown)}", file=sys.stderr)
            sys.exit(2)
        tasks = [t for t in tasks if t["id"] in wanted]
    # Report missing fixtures **before** building the agent. The same rule as for the
    # judge in `testprompt.py`: what makes the run fail anyway belongs before the
    # expensive part — the late report cost model start and warm-up, measured.
    missing = sorted({f for t in tasks for f in t.get("fixtures", [])
                      if not (FIXTURES / f).is_file()})
    if missing:
        print(
            f"Fixtures fehlen ({', '.join(missing)}).\n"
            "Neu erzeugen:  uv run python probes/make_fixtures.py",
            file=sys.stderr,
        )
        sys.exit(2)
    timeout = args.timeout
    if timeout is None:
        timeout = TEAM_TIMEOUT_S if agent_kind() == "team" else DEFAULT_TIMEOUT_S
    print(f"Agent: {agent_kind()} · Zeitdeckel je Probe {timeout:.0f}s")
    sys.exit(asyncio.run(run_all(tasks, args.verbose or bool(args.task_id), timeout)))


if __name__ == "__main__":
    main()
