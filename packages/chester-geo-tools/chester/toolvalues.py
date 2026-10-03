"""The numbers a tool returned — written down by the team, read back by the measurement.

Test-Level 2 grades a computed value by finding it in a **tool return**, never in the
answer text: a number in prose is no artifact (`chester.probes`, kind ``value_seen``).
For the single agent every tool return passes the runner. For chester-team it does
not — the tool computes *inside* a ressort, and the ressort's return carries a report
(prose), its outputs (paths) and the tool names, but not what the tools answered.
Measured 2026-09-26, first full team run on Test-Level 2: `area-in-degrees`,
`footprint-area-sum` and `height-gini` failed although ressort and tool choice were
right, because the number never reached the check. Tool *names* had the same gap and
were fixed on 2026-09-19 (`chester.toolchoice.inner_tools`); this is the counterpart
for *values*.

The numbers go into the ressort's log (``team-runs/ressort-calls.jsonl``, one entry
per inner call), not into its return. The return is what the orchestrator's model
reads: numbers there cost context on every call and lay out figures the model could
mistake for checked results. The log already holds the *why* of each call; it now
holds the *what* as well, and the measurement reads it from there.

Both halves live here because the writer (chester-team) and the reader (chester-agent)
must not import each other, and both may import this package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: How deep a tool return is walked — a GeoJSON coordinate list is no result.
MAX_DEPTH = 6
#: Numbers kept per call in the log. A statistics return holds a handful; a return
#: with thousands (a histogram, a coordinate list) is not a result to grade either.
LOG_LIMIT = 200


def numbers(obj: Any, _depth: int = 0) -> list[float]:
    """Every number in a tool return, nested to any depth up to `MAX_DEPTH`.

    Strings are skipped on purpose, numerals included: a number in prose is no artifact.
    """
    if _depth > MAX_DEPTH or isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in numbers(v, _depth + 1)]
    if isinstance(obj, (list, tuple)):
        return [n for v in obj for n in numbers(v, _depth + 1)]
    return []


def logged(content: Any) -> list[float]:
    """The numbers of one inner tool return, as the ressort log keeps them."""
    return numbers(content)[:LOG_LIMIT]


def ressort_numbers(tool_results: list[Any]) -> list[float]:
    """The numbers the tools *inside* the ressorts returned — empty for a single agent.

    Each ressort return names its log file; the entry for this very call is found by
    what the return and the entry share (ressort, tool names, duration), so no clock
    and no time window are involved. A missing or unreadable log yields nothing — the
    check then fails as it did before, it does not pass by accident.
    """
    out: list[float] = []
    for r in tool_results:
        if isinstance(r, str) and r.lstrip().startswith("{"):
            try:
                r = json.loads(r)
            except ValueError:
                continue
        if not (isinstance(r, dict) and "ressort" in r and r.get("log")):
            continue
        entry = _entry(str(r["log"]), r)
        for call in (entry or {}).get("calls") or []:
            out.extend(float(v) for v in call.get("values") or [])
    return out


def _entry(log: str, result: dict) -> dict | None:
    """The log line written for this ressort return (the last one, should two match)."""
    try:
        lines = Path(log).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    fields = ("ressort", "tools_called", "duration_s")
    key = [result.get(f) for f in fields]
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and [entry.get(f) for f in fields] == key:
            return entry
    return None
