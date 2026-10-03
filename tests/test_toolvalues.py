"""`value_seen` sees into a ressort — through its log, not through its handover.

Measured 2026-09-26: three probes failed against chester-team although ressort and tool
choice were right, because the number was computed inside a ressort and only prose,
paths and tool names came back. These tests drive the real writer (`_outcome`,
`_write_log`) and the real reader (`chester.probes.check`) against each other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart

from chester import toolvalues
from chester.probes import check
from chester.team import ressorts

GINI = {"kind": "value_seen", "expect": 0.2222, "tol_abs": 0.01}


def _ressort_call(ws: Path, content: dict, *, duration: float = 41.3,
                  report: str = "Done.") -> dict:
    """One ressort call as the orchestrator sees it, with its log written for real."""
    outcome = ressorts._outcome(ToolReturnPart(tool_name="geo_python_run", content=content,
                                               tool_call_id="c1"))
    result = {"ok": True, "ressort": "vector", "outputs": [], "report": report,
              "tools_called": ["geo_python_run"], "duration_s": duration}
    result["log"] = ressorts._write_log(str(ws), "gini of the heights", result, [outcome])
    return result


def test_a_number_inside_a_ressort_is_seen(tmp_path):
    handover = _ressort_call(tmp_path, {"ok": True, "gini": 0.2251})
    assert "0.2251" not in json.dumps(handover), "the number must not ride the handover"
    ok, why = check(GINI, workspace=tmp_path, tool_results=[handover])
    assert ok, why


def test_a_persisted_handover_as_json_text_is_read_too(tmp_path):
    handover = _ressort_call(tmp_path, {"ok": True, "gini": 0.2251})
    ok, why = check(GINI, workspace=tmp_path, tool_results=[json.dumps(handover)])
    assert ok, why


def test_the_ressorts_prose_still_does_not_count(tmp_path):
    """The rule of `value_seen` holds for the team as well: a report is no artifact."""
    handover = _ressort_call(tmp_path, {"ok": True, "output": "x.gpkg"},
                             report="The Gini coefficient is 0.2251.")
    ok, _ = check(GINI, workspace=tmp_path, tool_results=[handover])
    assert not ok


def test_only_this_calls_entry_is_read(tmp_path):
    """The log holds every ressort call of every run; another call's number must not
    pass this one's check."""
    _ressort_call(tmp_path, {"ok": True, "gini": 0.2251}, duration=12.0)
    this = _ressort_call(tmp_path, {"ok": True, "gini": 0.31}, duration=55.5)
    ok, _ = check(GINI, workspace=tmp_path, tool_results=[this])
    assert not ok


def test_a_missing_log_fails_instead_of_passing(tmp_path):
    handover = {"ressort": "vector", "tools_called": [], "duration_s": 1.0,
                "log": str(tmp_path / "gone.jsonl")}
    assert toolvalues.ressort_numbers([handover]) == []


def test_a_single_agent_has_no_ressort_numbers():
    assert toolvalues.ressort_numbers([{"ok": True, "sum": 1576.0}, "prose 3"]) == []


@pytest.mark.parametrize("content, expected", [
    ({"a": 1, "b": [2.5, {"c": 3}], "flag": True, "text": "4"}, [1.0, 2.5, 3.0]),
    ("1576", []),
])
def test_numbers_skips_booleans_and_strings(content, expected):
    assert toolvalues.numbers(content) == expected


def test_the_log_keeps_a_bounded_number_of_values():
    outcome = ressorts._outcome(ToolReturnPart(tool_name="raster_histogram",
                                               content={"counts": list(range(5000))},
                                               tool_call_id="c1"))
    assert len(outcome["values"]) == toolvalues.LOG_LIMIT


def test_a_rejected_call_logs_no_values():
    outcome = ressorts._outcome(RetryPromptPart(content="bad args", tool_name="vector_info"))
    assert not outcome.get("values")
