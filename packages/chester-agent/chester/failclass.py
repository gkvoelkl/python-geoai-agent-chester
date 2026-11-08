"""Why a bank run failed — one or more classes per FAIL, read off the run without a model (MB.2).

After GISAgentBench (arXiv 2608.01645), which sorted 1 381 failed runs: a required
operation missing 28 %, operations in the wrong order 18 %, redundant or looping steps
14 %, a wrong tool substituted 13 %, output never written 10 %, a parameter or formula
error 8 %. A FAIL alone says *that* a run went wrong; the distribution says where the
next lever is — the instructions, a guard, a tool description.

Read from what every judged run already leaves: the history line (`tools_called`,
`tools_expected`) and, when it still exists, the run's protocol (the ``← tool: {...}``
results). Pure stdlib, no agent stack — like `chester.evalhistory`.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path

MISSING = "Operation fehlt"
ORDER = "falsche Reihenfolge"
LOOP = "Schleife"
SUBSTITUTE = "falsches Werkzeug"
PARAMETER = "Parameterfehler"
CLASSES = (MISSING, ORDER, LOOP, SUBSTITUTE, PARAMETER)

#: Expected tool → tools that stand in for it wrongly. Each pair is a measured mistake:
#: a radius for a travel time (2026-09-21), Copernicus for DGM1 (2026-10-26, 11-08), a
#: web search or OSM levels for LoD2 heights (2026-09-27).
SUBSTITUTES = {
    "service_area": {"vector_buffer"},
    "fetch_dgm1": {"fetch_dem"},
    "fetch_lod2": {"duckduckgo_search", "web_fetch"},
}

#: The same tool this many times in a row is a loop, not a chain.
LOOP_RUN = 4

_RESULT = re.compile(r"← ([a-z_0-9]+): (\{.*)$")


def _list(value) -> list[str]:
    """History fields are stored as ``str(list)`` — parse them back."""
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = ast.literal_eval(str(value))
    except (ValueError, SyntaxError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def failed_results(log_path: str | Path) -> list[str]:
    """Tools whose **last** result in the run protocol says ``"ok": false``.

    A failed call the model then corrected is the normal way of working, not a cause;
    a tool that never succeeded after its error is one.
    """
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    last: dict[str, bool] = {}
    for line in text.splitlines():
        match = _RESULT.search(line)
        if match:
            last[match.group(1)] = '"ok": false' not in match.group(2)
    return sorted(tool for tool, ok in last.items() if not ok)


def classify(called: list[str], expected: list[str], failed: list[str] | None = None,
             ) -> list[str]:
    """The failure classes of one run. ``expected`` entries may name alternatives (``a|b``)."""
    classes = []
    groups = [set(e.split("|")) for e in expected]
    missing = [g for g in groups if not g & set(called)]
    if missing:
        classes.append(MISSING)
    if any(g & SUBSTITUTES.get(t, set()) for g in [set(called)] for m in missing for t in m):
        classes.append(SUBSTITUTE)
    firsts = [min(i for i, t in enumerate(called) if t in g) for g in groups if g & set(called)]
    if firsts != sorted(firsts):
        classes.append(ORDER)
    run = longest = 1
    for a, b in zip(called, called[1:], strict=False):
        run = run + 1 if a == b else 1
        longest = max(longest, run)
    if called and longest >= LOOP_RUN:
        classes.append(LOOP)
    if failed:
        classes.append(PARAMETER)
    return classes


def _passed(record: dict) -> bool:
    return str(record.get("passed")).lower() == "true"


def classify_record(record: dict) -> list[str]:
    """The classes of one history line — empty for a passed run."""
    if _passed(record):
        return []
    log = record.get("log")
    return classify(_list(record.get("tools_called")), _list(record.get("tools_expected")),
                    failed_results(log) if log else None)


def report(records: list[dict], filter: str | None = None) -> str:  # noqa: A002  # as format_report
    """The distribution over failed runs, as text for `evals.py --report`."""
    rows = [r for r in records if not filter
            or filter in str(r.get("test_id", "")) or filter in str(r.get("model", ""))]
    failed = [r for r in rows if not _passed(r)]
    if not failed:
        return ""
    counts = Counter(c for r in failed for c in classify_record(r))
    unclassified = sum(1 for r in failed if not classify_record(r))
    parts = [f"{n}× {c}" for c, n in counts.most_common()]
    if unclassified:
        parts.append(f"{unclassified}× ohne Klasse (Fachlogik — Werkzeuge stimmten)")
    return (f"\n\n_Fehlerklassen ({len(failed)} FAIL, ein Lauf kann mehrere tragen):_ "
            + " · ".join(parts))


if __name__ == "__main__":
    from chester.evalhistory import load_history

    print(report(load_history()).strip() or "keine FAIL-Läufe")
    print(json.dumps(Counter(c for r in load_history() for c in classify_record(r)),
                     ensure_ascii=False))
