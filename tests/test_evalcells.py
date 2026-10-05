"""The cell assignment of the measurement series — Test-Level 1 (pure, no network, no model).

The heart of it is in two tests: a run without a label does **not** become the base
cell, and a wrongly labelled record reports itself. Both are the lesson from KQ §2e —
"a rule without a field does not hold".
"""

from __future__ import annotations

import pytest

from chester.evalcells import (
    by_cell,
    cell_label,
    cells_present,
    format_cells,
    label_warnings,
    normalise_cell,
    per_test_cells,
    run_conditions,
)


def _run(test_id: str, cell: str | None, passed: bool, *, model: str = "ollama/gemma4:26b-mlx",
         coverage: float | None = 0.5, per_step: float | None = 1.2,
         use_qgis: bool | None = False) -> dict:
    return {"test_id": test_id, "cell": cell, "passed": passed, "model": model,
            "tool_coverage": coverage, "calls_per_step": per_step,
            "duration_s": 600.0, "use_qgis": use_qgis}


def test_a_run_without_a_label_stays_unknown():
    """Die Kernregel: fehlendes Feld heisst *unbekannt*, nie ``L+``."""
    assert cell_label({}) is None
    assert cell_label({"CHESTER_EVAL_CELL": "   "}) is None
    assert run_conditions({})["cell"] is None


def test_the_label_survives_case_and_a_typographic_minus():
    assert normalise_cell(" l+ ") == "L+"
    assert normalise_cell("L−") == "L-"  # U+2212, copied from the German prose
    assert cell_label({"CHESTER_EVAL_CELL": "f+"}) == "F+"


def test_run_conditions_record_the_toolbox_switch(monkeypatch):
    monkeypatch.setenv("CHESTER_NO_QGIS", "1")
    monkeypatch.setenv("CHESTER_EVAL_CELL", "L+")
    monkeypatch.delenv("CHESTER_AGENT", raising=False)
    assert run_conditions() == {"cell": "L+", "use_qgis": False, "agent": "agent"}


def test_the_agent_kind_is_recorded_and_checked():
    from chester.evalcells import agent_kind

    assert agent_kind({}) == "agent"
    assert agent_kind({"CHESTER_AGENT": " Team "}) == "team"
    with pytest.raises(ValueError, match="CHESTER_AGENT"):
        agent_kind({"CHESTER_AGENT": "swarm"})


def test_unlabelled_runs_are_left_out_of_the_cell_tables():
    records = [_run("a", "L+", True), _run("a", None, True), _run("a", None, False)]
    assert cells_present(records) == ["L+"]
    assert by_cell(records)["L+"] == {
        "runs": 1, "passed": 1, "tests": 1, "avg_coverage": 0.5,
        "avg_per_step": 1.2, "avg_duration": 600.0,
        "models": ["ollama/gemma4:26b-mlx"],
    }


def test_an_ungraded_run_counts_in_neither_column():
    """``passed: None`` is ungraded — counted as FAIL it would falsify the comparison."""
    records = [_run("a", "L+", True), {**_run("a", "L+", False), "passed": None}]
    assert by_cell(records)["L+"]["runs"] == 1


def test_per_test_shows_both_cells_as_fractions():
    records = [
        _run("buffer-schools-500m", "L+", True),
        _run("buffer-schools-500m", "L+", False),
        _run("buffer-schools-500m", "L+", True),
        _run("buffer-schools-500m", "F+", True, model="anthropic/claude-sonnet-5"),
    ]
    rows = per_test_cells(records)
    assert len(rows) == 1
    cells = rows[0]["cells"]
    assert (cells["L+"]["passed"], cells["L+"]["runs"]) == (2, 3)
    assert (cells["F+"]["passed"], cells["F+"]["runs"]) == (1, 1)


def test_one_cell_alone_renders_nothing():
    """A single column would look like a result and is none."""
    assert format_cells([_run("a", "L+", True)]) == ""


def test_the_table_appears_once_a_second_cell_has_runs():
    records = [_run("a", "L+", True), _run("a", "F+", False, model="anthropic/claude-sonnet-5")]
    out = format_cells(records)
    assert "L+ vs F+" in out
    assert "| `a` | 1/1 | 0/1 |" in out


def test_a_cell_holding_two_models_is_flagged():
    records = [_run("a", "L+", True), _run("a", "L+", True, model="anthropic/claude-sonnet-5")]
    assert any("cell L+ holds 2 models" in w for w in label_warnings(records))


def test_a_model_in_two_cells_is_flagged_unless_it_is_the_tool_axis():
    shared = [_run("a", "L+", True), _run("a", "L-", False)]
    assert not [w for w in label_warnings(shared) if "appears in cells" in w]
    crossed = [_run("a", "L+", True), _run("a", "F+", False)]
    assert any("appears in cells" in w for w in label_warnings(crossed))


def test_a_toolbox_switch_under_the_series_is_flagged():
    records = [_run("a", "L+", True), _run("b", "L+", True, use_qgis=True)]
    assert any("use_qgis" in w for w in label_warnings(records))


def test_unlabelled_runs_are_reported_next_to_a_labelled_series():
    records = [_run("a", "L+", True), _run("b", None, True)]
    assert any("carry no cell" in w for w in label_warnings(records))


def test_an_archive_without_any_label_says_nothing():
    """The old history (before this file) must not appear as a finding."""
    assert label_warnings([_run("a", None, True)]) == []
    assert format_cells([_run("a", None, True)]) == ""


def test_every_running_cell_is_a_known_label():
    """A running cell that is not in the directory lands at the back of the report.

    That was exactly the case until 14.09.2026: **F−** had been running since 13.09. but
    was not in `KNOWN_CELLS` — its runs would have been reported as an unknown label and
    sorted behind the known columns. The test reads the cells from the published concept
    table so prose and code do not drift apart.
    """
    import re
    from pathlib import Path

    from chester.evalcells import KNOWN_CELLS, normalise_cell

    doc = Path(__file__).resolve().parent.parent / "doc" / "tool-compensation.md"
    # Only cell names: L/F, then plus or minus, then also `/Produkt` (the breadth row).
    # Otherwise the pattern catches the bold column headers of other tables — **or it
    # misses the F− rows**, which almost happened on 14.09.: the test stayed green and
    # simply no longer checked them.
    zellen = {
        normalise_cell(m.group(1))
        for m in re.finditer(r"^\| \*\*([LF][+−-][\w+/]*)\*\* \|",
                             doc.read_text(), re.MULTILINE)
    }
    assert zellen, "die Zellentabelle wurde nicht gefunden"
    fehlend = zellen - set(KNOWN_CELLS)
    assert not fehlend, f"Zellen im Konzept, aber nicht in KNOWN_CELLS: {sorted(fehlend)}"


def test_a_team_cell_must_come_from_the_team():
    from chester.evalcells import label_warnings

    records = [_run("a", "L+TEAM", True) | {"agent": "agent"},
               _run("b", "L+", True) | {"agent": "team"},
               _run("c", "L+TEAM", True) | {"agent": "team"}]
    warnings = " ".join(label_warnings(records))
    assert "L+TEAM ran as agent" in warnings and "L+ ran as team" in warnings
    assert "L+TEAM ran as team" not in warnings
