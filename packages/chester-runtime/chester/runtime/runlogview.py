"""Reading the run log back — the record a run leaves even when it never finishes.

`RunLogCapability` appends one line per tool call, result and model reply while the run
happens. SelmaKit writes the session file only when a turn *ends*: a run that is cut off
(a probe's time limit, a crash, a kill) leaves no session, and `trace.py <key>` used to
answer "No such session" — for exactly the runs one most wants to read. Measured
2026-08-16 (`dop-ndvi-no-nir-bayern`) and reproducible on every probe that hits its
time limit since 2026-09-01. The log was there all along; only nothing read it after
the fact. This does, and `trace.py live` shares the line format with it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from chester.runtime.runlog import DEFAULT_LOG_DIR, _safe_name

#: A pause longer than this separates two runs in one log. The log is cumulative per
#: session key (every probe run appends to `probe_<id>.jsonl`), and a single run's
#: longest silence — one slow model call — stays well under it.
RUN_GAP_MINUTES = 20

_ARROWS = {"call": "→", "result": "←", "error": "✗", "invalid": "⊘"}


def log_path(session_key: str, log_dir: str = DEFAULT_LOG_DIR) -> Path:
    """Where `RunLogCapability` writes the log for this session key."""
    return Path(log_dir) / f"{_safe_name(session_key)}.jsonl"


def read_records(path: Path) -> list[dict[str, Any]]:
    """Every readable line; a half-written last line is skipped, not fatal."""
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def last_run(records: list[dict[str, Any]], gap_minutes: int = RUN_GAP_MINUTES) -> list[dict]:
    """The records of the most recent run: everything after the last long pause.

    A pause that ends in a tool's *result* or *error* is not a boundary — the call was
    still running. Without that rule the run of 2026-10-04, hung 2 h 23 min inside one
    `geo_python_run`, lost everything before the hang.
    """
    start = 0
    for i in range(1, len(records)):
        if records[i].get("kind") in ("result", "error"):
            continue
        try:
            a = datetime.fromisoformat(records[i - 1]["t"])
            b = datetime.fromisoformat(records[i]["t"])
        except (KeyError, ValueError):
            continue
        if (b - a).total_seconds() > gap_minutes * 60:
            start = i
    return records[start:]


def format_record(r: dict[str, Any]) -> str:
    """One log record as one readable line (shared with `trace.py live`)."""
    if r.get("kind") == "text":
        # `repeats` flags a degenerate reply at a glance: healthy answers sit at 1-2,
        # a model looping on one line runs into the hundreds.
        flag = "  ⚠ REPETITION" if r.get("repeats", 0) > 5 else ""
        return (f"{r.get('t', '')[11:]} 💬 {'':<22}{r.get('chars', 0):>6} Z."
                f"  {r.get('text', '')[:100]}{flag}")
    body = r.get("args") or r.get("result") or r.get("error") or ""
    secs = f"{r['seconds']:>6.1f}s" if r.get("seconds") is not None else " " * 7
    return (f"{r.get('t', '')[11:]} {_ARROWS.get(str(r.get('kind')), ' ')} "
            f"{r.get('tool', ''):<22}{secs}  {body[:110]}")
