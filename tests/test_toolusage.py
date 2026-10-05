"""The tool-usage ledger — which tool ran when, kept across sessions (Test-Level 1).

The ledger exists because every other record of a call is temporary: session traces
are deleted before each bench run, probe and dialogue, so the coverage sensor counted
38 tools as never called on 2026-10-05 when 29 was right. These tests pin the three
things that made the other records unfit: it only grows, it never breaks a run, and
it says how a call ended — not only that it happened.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from chester import toolusage
from chester.runtime.usagelog import ToolUsageCapability


def _ctx(key="s1"):
    return SimpleNamespace(deps=key)


def _call(cid="c1"):
    return SimpleNamespace(tool_call_id=cid)


def _tool(name="geocode"):
    return SimpleNamespace(name=name)


def test_record_and_read_round_trip(tmp_path):
    toolusage.record("geocode", "ok", seconds=0.4, session="s1", log_dir=tmp_path)
    toolusage.record("vector_clip", "fail", log_dir=tmp_path)
    entries = toolusage.read(tmp_path)
    assert [e["tool"] for e in entries] == ["geocode", "vector_clip"]
    assert entries[0]["seconds"] == 0.4 and entries[0]["session"] == "s1"
    assert entries[0]["ts"].endswith("+00:00")  # the ledger stays UTC


def test_the_ledger_only_grows(tmp_path):
    """Nothing rewrites it — two writers append whole lines."""
    for _ in range(3):
        toolusage.record("geocode", "ok", log_dir=tmp_path)
    assert len((tmp_path / toolusage.LEDGER).read_text().splitlines()) == 3


def test_ok_false_is_a_failure_not_a_success():
    """`ok: false` is a result, not an exception — as success it would hide it."""
    assert toolusage.outcome_of({"ok": False, "error": "x"}) == "fail"
    assert toolusage.outcome_of({"ok": True}) == "ok"
    assert toolusage.outcome_of("plain text") == "ok"


def test_usage_counts_calls_failures_and_last_use():
    entries = [
        {"ts": "2026-10-01T10:00:00+00:00", "tool": "geocode", "outcome": "ok"},
        {"ts": "2026-10-03T10:00:00+00:00", "tool": "geocode", "outcome": "error"},
        {"ts": "2026-10-02T10:00:00+00:00", "tool": "slope", "outcome": "unknown"},
    ]
    use = toolusage.usage(sorted(entries, key=lambda e: e["ts"]))
    assert use["geocode"] == {"calls": 2, "not_ok": 1, "first": "2026-10-01T10:00:00+00:00",
                              "last": "2026-10-03T10:00:00+00:00"}
    assert use["slope"]["calls"] == 1
    assert use["slope"]["not_ok"] == 0, "backfilled history is not a failure"


def test_times_are_shown_in_local_time():
    """The user reads Europe/Berlin; the file keeps UTC."""
    assert toolusage.local_time("2026-10-05T10:00:00+00:00") == "05.10.2026 12:00"
    assert toolusage.local_time(None) == "—"


def test_a_torn_last_line_costs_one_entry(tmp_path):
    toolusage.record("geocode", "ok", log_dir=tmp_path)
    with open(tmp_path / toolusage.LEDGER, "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-10-05T1')
    assert [e["tool"] for e in toolusage.read(tmp_path)] == ["geocode"]


def test_an_unwritable_ledger_does_not_break_the_run():
    toolusage.record("geocode", "ok", log_dir="/proc/nope/definitely-not-writable")


def test_tests_do_not_write_the_real_ledger(tmp_path, monkeypatch):
    """A test driving a real agent must not count as usage of the toolbox."""
    monkeypatch.chdir(tmp_path)
    toolusage.record("geocode", "ok")
    assert not (tmp_path / toolusage.DEFAULT_LOG_DIR / toolusage.LEDGER).exists()


def test_the_main_table_names_tools_and_last_use(tmp_path, capsys):
    toolusage.record("geocode", "ok", log_dir=tmp_path)
    assert toolusage.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "geocode" in out and "1 calls of 1 tools" in out


# ── the observer in the agent ───────────────────────────────────────────


def _ledger(tmp_path):
    return [json.loads(x) for x in (tmp_path / toolusage.LEDGER).read_text().splitlines()]


def test_the_observer_records_outcome_and_duration(tmp_path):
    cap = ToolUsageCapability(log_dir=str(tmp_path))
    asyncio.run(cap.before_tool_execute(_ctx(), call=_call(), tool_def=_tool(), args={}))
    result = {"ok": False}
    assert asyncio.run(
        cap.after_tool_execute(_ctx(), call=_call(), tool_def=_tool(), args={}, result=result)
    ) is result
    entry = _ledger(tmp_path)[-1]
    assert entry["tool"] == "geocode" and entry["outcome"] == "fail"
    assert entry["seconds"] is not None and entry["session"] == "s1"


def test_the_observer_re_raises_tool_errors(tmp_path):
    cap = ToolUsageCapability(log_dir=str(tmp_path))
    with pytest.raises(ValueError, match="kaputt"):
        asyncio.run(cap.on_tool_execute_error(
            _ctx(), call=_call(), tool_def=_tool(), args={}, error=ValueError("kaputt")))
    assert _ledger(tmp_path)[-1]["outcome"] == "error"


def test_the_observer_re_raises_validation_errors(tmp_path):
    cap = ToolUsageCapability(log_dir=str(tmp_path))
    with pytest.raises(ValueError):
        asyncio.run(cap.on_tool_validate_error(
            _ctx(), call=_call(), tool_def=_tool("write_plan"), args={}, error=ValueError("x")))
    assert _ledger(tmp_path)[-1] == {**_ledger(tmp_path)[-1], "tool": "write_plan",
                                     "outcome": "invalid"}


def test_every_agent_carries_the_observer():
    """Wired into the base set, so the single agent and every ressort agent write it."""
    from chester.runtime.wiring import base_capabilities

    kinds = [type(c).__name__ for c in base_capabilities()]
    assert "ToolUsageCapability" in kinds


def test_ressort_agents_write_the_ledger_too(tmp_path, monkeypatch):
    """chester-team's ressort agents are plain pydantic-ai agents — no base set. Until
    2026-10-05 their calls reached neither the run log nor any count: `ruggedness`,
    `rasterize` and `service_area` ran only there and looked unused."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from chester.team import ressorts

    seen = []
    monkeypatch.setattr(toolusage, "record", lambda tool, outcome, **kw: seen.append(
        (tool, outcome, kw.get("session"))))

    def respond(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("vector_info", {"path": "none.gpkg"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "outputs": [], "report": "done", "open_points": []})])

    geodata = {"roots": [], "postgis": None, "stac_catalogs": None, "ttl_by_source": {}}
    agent = ressorts.build_ressort_agent("vector", str(tmp_path), model=FunctionModel(respond),
                                         geodata=geodata)
    asyncio.run(ressorts.run_ressort("vector", "inspect", workspace=str(tmp_path), agent=agent))
    assert ("vector_info", seen[0][1], "ressort-vector") == seen[0]
