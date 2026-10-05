"""The log capability — an observer that must not change the run.

Occasion (2026-09-04): after a user complaint a dialogue turn switched to the right
method, computed `grass:r.watershed`, wrote a map — and then died at the request limit.
SelmaKit writes the session at the *end* of a turn, so afterwards no log of any of it
existed. This capability appends every line immediately.

Two properties matter more here than the content: the hooks are pass-throughs, and the
error hook **re-raises**. Its contract reads "return any value to suppress the error and
use it as the tool result" — a `return None` would have silently swallowed every tool
error.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from chester.runtime.runlog import RunLogCapability, _safe_name, _short


def _ctx(key="s1"):
    return SimpleNamespace(deps=key)


def _call(cid="c1"):
    return SimpleNamespace(tool_call_id=cid)


def _tool(name="geocode"):
    return SimpleNamespace(name=name)


def _lines(tmp_path, key="s1"):
    path = tmp_path / f"{key}.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_a_call_is_written_before_the_tool_runs(tmp_path):
    """The point: a hanging tool is visible as such."""
    cap = RunLogCapability(log_dir=str(tmp_path))
    asyncio.run(cap.before_tool_execute(_ctx(), call=_call(), tool_def=_tool(), args={"q": "x"}))
    entries = _lines(tmp_path)
    assert [e["kind"] for e in entries] == ["call"]
    assert entries[0]["tool"] == "geocode"


def test_hooks_pass_their_payload_through_unchanged(tmp_path):
    """An observer that changes arguments or results is none."""
    cap = RunLogCapability(log_dir=str(tmp_path))
    args = {"q": "x"}
    result = {"ok": True, "n": 3}
    assert asyncio.run(
        cap.before_tool_execute(_ctx(), call=_call(), tool_def=_tool(), args=args)
    ) is args
    assert asyncio.run(
        cap.after_tool_execute(_ctx(), call=_call(), tool_def=_tool(), args=args, result=result)
    ) is result


def test_a_tool_error_is_logged_and_re_raised(tmp_path):
    """The dangerous spot: returning instead of raising would suppress the error."""
    cap = RunLogCapability(log_dir=str(tmp_path))
    boom = ValueError("kaputt")
    with pytest.raises(ValueError, match="kaputt"):
        asyncio.run(
            cap.on_tool_execute_error(
                _ctx(), call=_call(), tool_def=_tool(), args={}, error=boom
            )
        )
    entry = _lines(tmp_path)[-1]
    assert entry["kind"] == "error"
    assert "ValueError: kaputt" in entry["error"]


def test_the_elapsed_time_pairs_call_and_result(tmp_path):
    cap = RunLogCapability(log_dir=str(tmp_path))
    asyncio.run(cap.before_tool_execute(_ctx(), call=_call("abc"), tool_def=_tool(), args={}))
    asyncio.run(
        cap.after_tool_execute(_ctx(), call=_call("abc"), tool_def=_tool(), args={}, result={})
    )
    assert _lines(tmp_path)[-1]["seconds"] is not None


def test_an_unwritable_directory_does_not_break_the_run(tmp_path):
    """An observer that can bring down the observed run is worse than none."""
    cap = RunLogCapability(log_dir="/proc/nope/definitely-not-writable")
    args = {"q": "x"}
    assert asyncio.run(
        cap.before_tool_execute(_ctx(), call=_call(), tool_def=_tool(), args=args)
    ) is args


def test_long_values_are_truncated_but_their_size_is_kept():
    """A `vector_info` on an OSM layer would otherwise flood the log."""
    out = _short("x" * 5000)
    assert len(out) < 1400
    assert "+3800 chars" in out


def test_session_keys_with_separators_become_one_filename():
    assert _safe_name("testprompt:pluvial-flow") == "testprompt_pluvial-flow"
    assert "/" not in _safe_name("a/b")


def test_the_model_reply_is_recorded_with_a_repetition_count(tmp_path):
    """The third text failure of 2026-09-04 went undocumented: the turn had finished a
    map and then repeated the link endlessly. Tool calls do not show that — there were
    21, all successful — and the abort prevented the session from being written.
    `repeats` turns the degeneration into a number."""
    cap = RunLogCapability(log_dir=str(tmp_path))
    resp = SimpleNamespace(
        parts=[SimpleNamespace(part_kind="text", content="Hier: /a.html\n" * 40)]
    )
    out = asyncio.run(cap.after_model_request(_ctx(), request_context=None, response=resp))
    assert out is resp, "die Antwort muss unverändert durchgereicht werden"
    entry = _lines(tmp_path)[-1]
    assert entry["kind"] == "text"
    assert entry["repeats"] == 40


def test_a_healthy_reply_has_a_low_repetition_count(tmp_path):
    cap = RunLogCapability(log_dir=str(tmp_path))
    resp = SimpleNamespace(
        parts=[SimpleNamespace(part_kind="text", content="Die Fläche beträgt 11,54 km².")]
    )
    asyncio.run(cap.after_model_request(_ctx(), request_context=None, response=resp))
    assert _lines(tmp_path)[-1]["repeats"] == 1


def test_a_reply_without_text_writes_nothing(tmp_path):
    """A pure tool answer is no text — otherwise every turn would carry an empty line."""
    cap = RunLogCapability(log_dir=str(tmp_path))
    resp = SimpleNamespace(parts=[SimpleNamespace(part_kind="tool-call", content=None)])
    asyncio.run(cap.after_model_request(_ctx(), request_context=None, response=resp))
    assert _lines(tmp_path) == []


def test_a_call_that_fails_validation_is_recorded_and_re_raised(tmp_path):
    """The blind spot that cost a whole log on 2026-09-05.

    `before_tool_execute` fires only **after** argument validation. The first call of the
    probe run was `write_plan` with `"id": 1`, where `PlanItem.id` requires a string —
    refused before execution, so no hook, so no file. From outside the run looked as if it
    had used no tool; in fact it had tried.
    """
    cap = RunLogCapability(log_dir=str(tmp_path))
    boom = ValueError("Input should be a valid string")
    with pytest.raises(ValueError, match="valid string"):
        asyncio.run(
            cap.on_tool_validate_error(
                _ctx(), call=_call(), tool_def=_tool("write_plan"),
                args={"items": [{"id": 1}]}, error=boom,
            )
        )
    entry = _lines(tmp_path)[-1]
    assert entry["kind"] == "invalid"
    assert entry["tool"] == "write_plan"
    assert "valid string" in entry["error"]


def test_the_validate_error_hook_must_not_swallow(tmp_path):
    """Counter-check of the contract: a return value would count as *validated*
    arguments and run the faulty call — the same trap as with
    `on_tool_execute_error`."""
    import inspect

    src = inspect.getsource(RunLogCapability.on_tool_validate_error)
    assert "raise error" in src
