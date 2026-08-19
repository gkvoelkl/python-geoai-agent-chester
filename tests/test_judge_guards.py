"""A run that cannot be read back must not be graded (testprompt).

Both guards come from one incident, 2026-08-16: a benchmark run wrote its Sentinel
bands, its NDVI map and a validation snapshot to the GeoCache over 954 s, but no
session file appeared. `read_trace` reported "no tools, no answer", the judge graded
that faithfully, and `history.jsonl` gained a FAIL whose stated reason — "the agent
produced no tool calls" — was false and completely convincing.

The rule these encode: a broken measurement must look broken, not like a finding.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from testprompt import TraceUnavailable, judge_run, read_trace, trace_from_protocol

# One real run that died mid-stream: `inspect_map` handed its snapshot to a text-only
# model, Ollama rejected the request, and SelmaKit — which persists only a *completed*
# run — wrote no session at all (2026-08-19, `walk-isochrone-hauptbahnhof`).
_ABORTED_PROTOCOL = """\
14:03:17 +  9.6s │ → geocode({"query":"Regensburg Hbf"})
14:03:17         │ ← geocode: {"ok": true, "display_name": "Regensburg Hauptbahnhof"}
14:09:27 + 68.5s │ → qgis_service_area({"minutes":15,"mode":"walk"})
14:09:36 +  9.3s │ ← qgis_service_area: {"ok": true, "reach_distance_m": 1125}
14:11:14 +  0.1s │ [run error: ModelHTTPError: status_code: 400, model_name: gemma4]
"""


def _judge_stub():
    """Stands in for the judge agent; fails the test if it is ever consulted."""

    class _Never:
        async def run(self, _prompt):  # pragma: no cover - reaching it is the failure
            raise AssertionError("judge was called for a transcript that cannot be graded")

    return _Never()


def test_missing_trace_raises_instead_of_reading_as_an_idle_run(monkeypatch, tmp_path):
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    with pytest.raises(TraceUnavailable) as exc:
        read_trace("never-persisted")
    # The message must point at the real suspect: reading, not the run.
    assert "never-persisted" in str(exc.value)


def test_unreadable_trace_raises_too(monkeypatch, tmp_path):
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    (tmp_path / "broken.json").write_text("{not json")
    with pytest.raises(TraceUnavailable):
        read_trace("broken")


def test_a_real_trace_still_reads(monkeypatch, tmp_path):
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    (tmp_path / "ok.json").write_text(
        json.dumps(
            [
                {"parts": [{"part_kind": "tool-call", "tool_name": "geocode"}]},
                {"parts": [{"part_kind": "text", "content": "56,6 km."}]},
            ]
        )
    )
    tools, answer = read_trace("ok")
    assert tools == ["geocode"] and answer == "56,6 km."


def test_a_crashed_run_is_graded_from_its_protocol(monkeypatch, tmp_path):
    """No session file, but the streamed protocol is right there — use it.

    Without this the 634 s of real geoprocessing in that run were unreadable and
    ungradable, purely because the turn ended in an exception instead of a result.
    """
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    tools, answer = read_trace("never-persisted", _ABORTED_PROTOCOL)
    assert tools == ["geocode", "qgis_service_area"]
    # The abort must be *stated*, not left as an empty answer the judge would read
    # as "the model said nothing" — that conflation is what this module guards.
    assert "aborted" in answer and "ModelHTTPError" in answer


def test_a_protocol_without_tools_or_error_still_raises(monkeypatch, tmp_path):
    """The fallback may not invent a transcript out of plain streamed text."""
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    with pytest.raises(TraceUnavailable):
        read_trace("never-persisted", "14:03:17 │ Ich schaue mir das an.\n")


def test_the_session_file_wins_when_both_exist(monkeypatch, tmp_path):
    """The persisted trace is the record; the protocol is only its stand-in."""
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    (tmp_path / "ok.json").write_text(
        json.dumps([{"parts": [{"part_kind": "text", "content": "56,6 km."}]}])
    )
    assert read_trace("ok", _ABORTED_PROTOCOL) == ([], "56,6 km.")


def test_protocol_parsing_ignores_tool_results():
    """Only the `→` call lines are the sequence; `←` results would double every tool."""
    tools, _ = trace_from_protocol(_ABORTED_PROTOCOL)
    assert tools == ["geocode", "qgis_service_area"]


def test_judge_refuses_an_empty_transcript():
    with pytest.raises(TraceUnavailable):
        asyncio.run(judge_run(_judge_stub(), {"id": "x"}, "prompt", [], ""))


def test_judge_refuses_whitespace_as_an_answer():
    with pytest.raises(TraceUnavailable):
        asyncio.run(judge_run(_judge_stub(), {"id": "x"}, "prompt", [], "   \n "))


def test_an_answer_without_tools_is_still_gradable():
    """Not every valid run calls a tool — refusing those would hide real failures."""

    class _Verdict:
        criteria: list = []
        passed = False
        reason = "no tools were needed but the answer is wrong"

    class _Judge:
        async def run(self, _prompt):
            return type("R", (), {"output": _Verdict()})()

    verdict, coverage, missing, effort = asyncio.run(
        judge_run(_Judge(), {"id": "x", "tools_expected": []}, "prompt", [], "eine Antwort")
    )
    assert verdict.passed is False
    assert effort["calls"] == 0
