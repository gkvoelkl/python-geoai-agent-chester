"""The sidebar's plan panel — a view on the event stream, not a second store.

The plan lives in the gateway process; the dashboard sees it only because every
``write_plan`` call runs through the SSE stream with its full payload. These tests pin
that evaluating this stream stays robust — in particular against the two forms arguments
arrive in (JSON text or already parsed), and against the roles ``cron``/``notification``
in the history.
"""

from __future__ import annotations

import json

from planpanel import _fallback_entries, _latest_plan


def _call(name: str, args) -> dict:
    return {"kind": "call", "name": name, "args": args}


def test_reads_the_plan_from_json_text_arguments():
    entries = [_call("write_plan", json.dumps({"items": [
        {"id": "1", "content": "DGM holen", "status": "completed"},
        {"id": "2", "content": "Senken füllen", "status": "in_progress"},
    ]}))]
    assert [i["id"] for i in _latest_plan(entries)] == ["1", "2"]


def test_reads_the_plan_from_already_parsed_arguments():
    entries = [_call("write_plan", {"items": [{"id": "1", "content": "x", "status": "pending"}]})]
    assert _latest_plan(entries)[0]["content"] == "x"


def test_the_last_write_plan_wins():
    """The point of the panel: state instead of a sequence of events."""
    entries = [
        _call("write_plan", {"items": [{"id": "1", "content": "alt", "status": "pending"}]}),
        _call("geocode", {"query": "Tegernheim"}),
        _call("write_plan", {"items": [{"id": "1", "content": "neu", "status": "completed"}]}),
    ]
    assert _latest_plan(entries)[0]["content"] == "neu"


def test_a_run_without_a_plan_yields_nothing():
    """Single-step questions need no plan — then the panel draws nothing."""
    assert _latest_plan([_call("geocode", {"query": "Regensburg"})]) == []
    assert _latest_plan([]) == []


def test_unparsable_arguments_do_not_raise():
    """A panel must not shoot down the chat; truncated arguments do occur."""
    assert _latest_plan([_call("write_plan", '{"items": [{"id": "1"')]) == []
    assert _latest_plan([_call("write_plan", None)]) == []


def test_fallback_takes_the_last_assistant_turn_not_the_last_message():
    """The history also carries `cron`/`notification`; taking the last message would
    empty the panel as soon as a notification arrives."""
    plan = [_call("write_plan", {"items": [{"id": "1", "content": "a", "status": "pending"}]})]
    messages = [
        {"role": "assistant", "tool_activity": plan},
        {"role": "notification", "content": "etwas passierte"},
    ]
    assert _latest_plan(_fallback_entries(messages))[0]["content"] == "a"


def test_fallback_is_empty_when_no_assistant_turn_has_activity():
    assert _fallback_entries([{"role": "user", "content": "hallo"}]) == ()
    assert _fallback_entries([]) == ()
