"""Tool choice on Test-Level 2 — did the agent reach for the right tool?

The baseline for chester-team (`internal/TODO.md`, KP.5 T0). The multi-agent concept
names two figures that only together show whether splitting the tools into ressorts
helps or merely moves the decision one level up:

* **tool hit rate** — was an expected tool called at all, *regardless of the result*;
* **ressort hit rate** — did the orchestrator load the expected ressort.

A single agent has no ressorts, so today only the first figure exists. It has to be
measured **before** the team does, or there is nothing to compare against. Each probe
therefore names its ``expected_tools`` (any one of them counts — a buffer via
``vector_buffer`` or ``qgis_buffer`` is the same choice) and its
``expected_ressort``, which the team run will be held to later.

Deliberately separate from the artifact checks in ``chester.probes``: a probe can pass
by a detour (``geo_python_run`` for everything) and still show a poor tool choice, and
that difference is exactly what the ressort cut is meant to change. The hit therefore
never decides ``passed``.

Pure — no model, no network; the runner passes in the names of the tools it saw.
"""

from __future__ import annotations

from typing import Any

from chester import ressortcut

#: The ressorts of the multi-agent concept, from the cut itself (chester.ressortcut) —
#: one source, so the probe bank and the ressort agents cannot name different ones.
RESSORTS = tuple(ressortcut.RESSORTS)


def tool_hit(task: dict, called: list[str], *, qgis: bool = True) -> bool | None:
    """Whether any expected tool was called — ``None`` when there is nothing to judge.

    Nothing to judge: the probe names no tools, or it is a QGIS probe
    (``needs_qgis``) and QGIS is off. Decided 2026-09-19: the ressorts work without
    QGIS, so the baseline runs with ``use_qgis: false`` — and a probe whose prompt
    demands `qgis_python` would otherwise count as a miss by construction.
    """
    expected = task.get("expected_tools") or []
    if not expected or (task.get("needs_qgis") and not qgis):
        return None
    return any(name in called for name in expected)


def describe(task: dict, called: list[str], *, qgis: bool = True) -> str:
    """One log line for the run, in the probe log's format."""
    hit = tool_hit(task, called, qgis=qgis)
    if hit is None:
        why = "needs QGIS, which is off" if task.get("expected_tools") else "not specified"
        return f"  · tool choice: not measured — {why}"
    seen = ", ".join(dict.fromkeys(called)) or "no tool"
    want = " | ".join(task["expected_tools"])
    mark = "✓" if hit else "✗"
    return f"  {mark} tool choice (not graded): expected {want} — called {seen}"


def hit_rate(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """``(hits, measured)`` over archived probe runs; rows without a verdict are skipped."""
    measured = [r for r in rows if r.get("tool_hit") is not None]
    return sum(1 for r in measured if r["tool_hit"]), len(measured)


def task_problems(task: dict, known_tools: set[str]) -> list[str]:
    """What is wrong with a probe's tool-choice fields — empty means fine."""
    problems = []
    unknown = [t for t in task.get("expected_tools") or [] if t not in known_tools]
    if unknown:
        problems.append(f"{task.get('id')}: unknown expected_tools {unknown}")
    ressort = task.get("expected_ressort")
    if ressort is not None and ressort not in RESSORTS:
        problems.append(f"{task.get('id')}: expected_ressort {ressort!r} not in {RESSORTS}")
    return problems
