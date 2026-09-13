"""Chester — which measurement *cell* a judged run belongs to (no LLM, no SelmaKit).

The compensation series (`doc/tool-compensation.md` §2) compares whole runs of the
bank against each other. Three cells are defined, and the model name alone does not
tell them apart:

- **L+** — local 26B model, full Chester. The base cell.
- **F+** — hosted frontier model, *the same* Chester. Differs from L+ in one value,
  ``model.model``.
- **L-** — the local model again, but with the guidance stripped (the tool axis,
  still to be built).

L+ and L- share a model, F+ shares the toolbox with both: no pair of cells is
separable from what the history already records. So the cell is a **label the
operator sets** (``CHESTER_EVAL_CELL``), never a guess made from the model name.

**A missing label means unknown, never L+.** That rule costs something — a night of
runs started without the variable is unassignable afterwards — and it is still the
right one: a run silently filed under the base cell would corrupt the very number
the series exists to produce. :func:`label_warnings` is the second half of that
bargain; it reads the archive back and says when a label looks mistyped.

Pure by design (`chester/*.py` imports no SelmaKit), so ``evals.py --report`` and
``/eval`` reach the same table without an agent stack.
"""

from __future__ import annotations

import os
from collections import defaultdict

#: The one way to name the current cell. Read per run, written into the record.
CELL_ENV = "CHESTER_EVAL_CELL"

#: The cells of `doc/tool-compensation.md` §2, in reading order — that is also the
#: column order of the report. A label outside this set is kept as written (the
#: archive records what happened, not what was expected) and flagged by
#: :func:`label_warnings`.
KNOWN_CELLS = ("L+", "F+", "L-")


def normalise_cell(raw: str | None) -> str | None:
    """``" l+ "`` → ``"L+"``, ``"L−"`` (U+2212) → ``"L-"``, blank → ``None``.

    Typing the cell by hand on a shell prompt is the whole input path, so the two
    ways to get it subtly wrong — case and the typographic minus that a copy out of
    the German prose carries — are absorbed here rather than left to split one cell
    into two in the report.
    """
    if raw is None:
        return None
    label = raw.strip().replace("−", "-").replace("–", "-")
    if not label:
        return None
    return label.upper()


def cell_label(env: dict[str, str] | None = None) -> str | None:
    """The cell of the run about to be recorded, or ``None`` when unset."""
    source = os.environ if env is None else env
    return normalise_cell(source.get(CELL_ENV))


def run_conditions(env: dict[str, str] | None = None) -> dict:
    """The measurement conditions that belong in every history record.

    ``cell`` is the operator's label, ``use_qgis`` the toolbox switch — the one
    setting that changes *which tools exist* without changing the model, and
    therefore the one that could otherwise move under a series unnoticed
    (`doc/tool-compensation.md` §2 names the configuration as part of the protocol).
    """
    from chester.qgis_env import qgis_disabled

    return {"cell": cell_label(env), "use_qgis": not qgis_disabled()}


def _graded(records: list[dict]) -> list[dict]:
    """Runs that carry a verdict. ``passed: None`` is unjudged, not failed."""
    return [r for r in records if r.get("passed") is not None]


def cells_present(records: list[dict]) -> list[str]:
    """The labelled cells in the archive, known ones first, then the rest sorted."""
    found = {normalise_cell(r.get("cell")) for r in _graded(records)}
    found.discard(None)
    known = [c for c in KNOWN_CELLS if c in found]
    return known + sorted(str(c) for c in found if c not in KNOWN_CELLS)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def by_cell(records: list[dict]) -> dict[str, dict]:
    """Per cell: runs, passes, mean coverage, mean calls-per-step, mean duration.

    Means skip the runs that lack the field — a row written before ``tool_calls``
    existed must not read as a run that made zero calls (same rule as
    :func:`chester.evalhistory.per_model`).
    """
    runs: dict[str, int] = defaultdict(int)
    passed: dict[str, int] = defaultdict(int)
    cov: dict[str, list[float]] = defaultdict(list)
    per_step: dict[str, list[float]] = defaultdict(list)
    dur: dict[str, list[float]] = defaultdict(list)
    models: dict[str, set[str]] = defaultdict(set)
    for r in _graded(records):
        cell = normalise_cell(r.get("cell"))
        if cell is None:
            continue
        runs[cell] += 1
        if r.get("passed"):
            passed[cell] += 1
        for field, bucket in (("tool_coverage", cov), ("calls_per_step", per_step),
                              ("duration_s", dur)):
            value = r.get(field)
            if value is not None:
                bucket[cell].append(float(value))
        models[cell].add(str(r.get("model", "?")))
    return {
        cell: {
            "runs": runs[cell],
            "passed": passed[cell],
            "tests": len({r.get("test_id") for r in _graded(records)
                          if normalise_cell(r.get("cell")) == cell}),
            "avg_coverage": _mean(cov[cell]),
            "avg_per_step": _mean(per_step[cell]),
            "avg_duration": _mean(dur[cell]),
            "models": sorted(models[cell]),
        }
        for cell in cells_present(records)
    }


def per_test_cells(records: list[dict], cells: list[str] | None = None) -> list[dict]:
    """Per test id, one entry per cell: ``passed/runs`` plus the two cost numbers.

    Fractions, not percentages: with three repetitions ``2/3`` is what was measured
    and ``67 %`` is a claim the data does not carry (`internal/TODO.md` KO.4).
    """
    columns = cells if cells is not None else cells_present(records)
    wanted = set(columns)
    rows: dict[str, dict[str, dict]] = defaultdict(dict)
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in _graded(records):
        cell = normalise_cell(r.get("cell"))
        if cell in wanted:
            grouped[(str(r.get("test_id", "?")), str(cell))].append(r)
    for (test_id, cell), runs in grouped.items():
        rows[test_id][cell] = {
            "runs": len(runs),
            "passed": sum(1 for r in runs if r.get("passed")),
            "avg_coverage": _mean([float(r["tool_coverage"]) for r in runs
                                   if r.get("tool_coverage") is not None]),
            "avg_per_step": _mean([float(r["calls_per_step"]) for r in runs
                                   if r.get("calls_per_step") is not None]),
        }
    return [{"test_id": test_id, "cells": rows[test_id]} for test_id in sorted(rows)]


def label_warnings(records: list[dict]) -> list[str]:
    """Where the labelling itself looks wrong — the guard against a mislabelled night.

    Three questions the archive can answer on its own: is a cell carrying two
    different models (L+ and F+ differ *by* the model, so that is a contradiction),
    is one model filed under two cells (correct for L+/L-, suspicious otherwise),
    and did a labelled series leave unlabelled runs behind.
    """
    out: list[str] = []
    graded = _graded(records)
    labelled = [r for r in graded if normalise_cell(r.get("cell")) is not None]
    if not labelled:
        return out
    unknown = [c for c in cells_present(records) if c not in KNOWN_CELLS]
    if unknown:
        out.append(f"cell label outside {list(KNOWN_CELLS)}: {unknown} — typo?")
    per_cell_models: dict[str, set[str]] = defaultdict(set)
    per_model_cells: dict[str, set[str]] = defaultdict(set)
    for r in labelled:
        cell = str(normalise_cell(r.get("cell")))
        model = str(r.get("model", "?"))
        per_cell_models[cell].add(model)
        per_model_cells[model].add(cell)
    for cell, models in sorted(per_cell_models.items()):
        if len(models) > 1:
            out.append(f"cell {cell} holds {len(models)} models: {sorted(models)} — "
                       "a cell is one model plus one toolbox")
    for model, cells in sorted(per_model_cells.items()):
        if len(cells) > 1 and sorted(cells) != ["L+", "L-"]:
            out.append(f"model {model} appears in cells {sorted(cells)} — "
                       "only L+/L- may share a model")
    switches = {bool(r["use_qgis"]) for r in labelled if r.get("use_qgis") is not None}
    if len(switches) > 1:
        out.append("labelled runs span both use_qgis: true and false — "
                   "the toolbox moved under the series")
    missing = len(graded) - len(labelled)
    if missing:
        out.append(f"{missing} judged run(s) carry no cell — unassignable, "
                   f"set {CELL_ENV} before the next batch")
    return out


def _fraction(entry: dict | None) -> str:
    return "-" if not entry else f"{entry['passed']}/{entry['runs']}"


def _pair(rows: dict[str, dict], columns: list[str], field: str, fmt: str) -> str:
    """The same number for every cell, joined by ``·`` — one column, not N."""
    parts = []
    for cell in columns:
        value = (rows.get(cell) or {}).get(field)
        parts.append("-" if value is None else format(value, fmt))
    return "·".join(parts)


def format_cells(records: list[dict]) -> str:
    """The two-cell view as Markdown, or ``""`` when there is nothing to compare.

    Empty for a single cell on purpose: until a second one has runs, the table says
    nothing that the by-model table above it does not already say, and an empty
    column reads like a result.
    """
    columns = cells_present(records)
    warnings = label_warnings(records)
    if len(columns) < 2:
        if warnings:
            return "\n".join(["_Cells:_", ""] + [f"- ⚠ {w}" for w in warnings])
        return ""
    totals = by_cell(records)
    joined = " · ".join(
        f"**{cell}** {totals[cell]['passed']}/{totals[cell]['runs']} "
        f"({', '.join(totals[cell]['models'])})"
        for cell in columns
    )
    cov_head = "cov " + "·".join(columns)
    step_head = "calls/step " + "·".join(columns)
    lines = [
        f"_Cells ({' vs '.join(columns)}):_ {joined}",
        "",
        "| test | " + " | ".join(columns) + f" | {cov_head} | {step_head} |",
        "|---" * (len(columns) + 3) + "|",
    ]
    for row in per_test_cells(records, columns):
        cells = row["cells"]
        fractions = " | ".join(_fraction(cells.get(cell)) for cell in columns)
        lines.append(
            f"| `{row['test_id']}` | {fractions} | "
            f"{_pair(cells, columns, 'avg_coverage', '.0%')} | "
            f"{_pair(cells, columns, 'avg_per_step', '.1f')} |"
        )
    for warning in warnings:
        lines.append("")
        lines.append(f"⚠ {warning}")
    return "\n".join(lines)
