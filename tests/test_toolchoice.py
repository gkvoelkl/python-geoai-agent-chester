"""Test-Level 2 bookkeeping for the tool-choice baseline (chester-team, KP.5 T0).

Asserts the pure figure and the probe bank's fields — not a model run. The run itself
is `uv run probe.py`; this keeps its bookkeeping honest.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from chester import toolchoice

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "agent-probe-tasks.jsonl"


def _known_tools() -> set[str]:
    """Every tool name the agent can have: the wrapper layer, the two agent-only
    escape hatches, and the QGIS tools (read from source — QGIS may be off here)."""
    from chester import mcpserver

    names = {t.__name__ for t in mcpserver.collect_tools("/tmp/chester-toolchoice")}
    names |= {"geo_python_run", "inspect_map"}
    caps = ROOT / "packages" / "chester-agent" / "chester" / "capabilities"
    for path in caps.glob("qgis*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("qgis_"):
                names.add(node.name)
    return names


def _tasks() -> list[dict]:
    return [json.loads(line) for line in TASKS.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_any_expected_tool_counts_as_a_hit():
    task = {"expected_tools": ["vector_buffer", "qgis_buffer"]}
    assert toolchoice.tool_hit(task, ["vector_info", "qgis_buffer"]) is True
    assert toolchoice.tool_hit(task, ["geo_python_run"]) is False
    assert toolchoice.tool_hit(task, []) is False


def test_a_probe_without_expectation_is_not_measured():
    assert toolchoice.tool_hit({}, ["vector_buffer"]) is None
    assert toolchoice.hit_rate([{"tool_hit": None}, {"tool_hit": True},
                                {"tool_hit": False}, {}]) == (1, 2)


def test_the_log_line_says_it_is_not_graded():
    line = toolchoice.describe({"expected_tools": ["vector_join"]}, ["vector_info"])
    assert line.lstrip().startswith("✗") and "not graded" in line and "vector_info" in line


def test_every_probe_names_a_real_tool_and_a_known_ressort():
    """A misspelt expected tool would read as a miss on every run — a false baseline."""
    known = _known_tools()
    assert len(known) > 80, "the tool inventory is suspiciously small"
    problems = [p for task in _tasks() for p in toolchoice.task_problems(task, known)]
    missing = [t["id"] for t in _tasks()
               if not t.get("expected_tools") or not t.get("expected_ressort")]
    assert not problems, problems
    assert not missing, f"probes without tool-choice fields: {missing}"


def test_task_problems_catches_typos():
    task = {"id": "x", "expected_tools": ["vector_bufer"], "expected_ressort": "vektor"}
    assert len(toolchoice.task_problems(task, {"vector_buffer"})) == 2


def test_a_qgis_probe_is_not_measured_without_qgis():
    task = {"expected_tools": ["qgis_python"], "needs_qgis": True}
    assert toolchoice.tool_hit(task, ["geo_python_run"], qgis=False) is None
    assert toolchoice.tool_hit(task, ["geo_python_run"], qgis=True) is False
    assert "QGIS" in toolchoice.describe(task, [], qgis=False)


def test_the_team_is_measured_inside_its_ressorts():
    """With the orchestrator, `called` holds ressort names; the tools that did the work
    come back in each ressort's `tools_called`."""
    called = ["write_plan", "ressort_vector", "check_crs"]
    results = [{"ok": True}, {"ressort": "vector", "tools_called": ["vector_reproject"]}]
    used = toolchoice.tools_used(called, results)
    assert used == ["write_plan", "check_crs", "vector_reproject"]
    task = {"expected_tools": ["vector_reproject"], "expected_ressort": "vector"}
    assert toolchoice.tool_hit(task, used) is True
    assert toolchoice.ressort_hit(task, called) is True
    assert toolchoice.ressort_hit(task, ["ressort_scout"]) is False


def test_a_single_agent_has_no_ressort_figure():
    task = {"expected_tools": ["vector_buffer"], "expected_ressort": "vector"}
    assert toolchoice.ressort_hit(task, ["vector_buffer"]) is None
    assert toolchoice.tools_used(["vector_buffer"], [{"ok": True}]) == ["vector_buffer"]
    assert toolchoice.hit_rate([{"ressort_hit": True}, {"ressort_hit": None}],
                               "ressort_hit") == (1, 1)
