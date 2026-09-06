"""Das Plan-Panel der Sidebar — eine Sicht auf den Ereignisstrom, kein zweiter Speicher.

Der Plan lebt im Gateway-Prozess; das Dashboard sieht ihn nur, weil jeder
``write_plan``-Aufruf mit vollständiger Nutzlast durch den SSE-Strom läuft. Diese
Tests halten fest, dass die Auswertung dieses Stroms robust bleibt — insbesondere
gegen die zwei Formen, in denen Argumente ankommen (JSON-Text oder schon geparst),
und gegen die Rollen ``cron``/``notification`` in der Historie.
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
    """Der Sinn des Panels: Zustand statt Ereignisfolge."""
    entries = [
        _call("write_plan", {"items": [{"id": "1", "content": "alt", "status": "pending"}]}),
        _call("geocode", {"query": "Tegernheim"}),
        _call("write_plan", {"items": [{"id": "1", "content": "neu", "status": "completed"}]}),
    ]
    assert _latest_plan(entries)[0]["content"] == "neu"


def test_a_run_without_a_plan_yields_nothing():
    """Einstufige Fragen brauchen keinen Plan — dann zeichnet das Panel nichts."""
    assert _latest_plan([_call("geocode", {"query": "Regensburg"})]) == []
    assert _latest_plan([]) == []


def test_unparsable_arguments_do_not_raise():
    """Ein Panel darf den Chat nicht abschießen; abgeschnittene Argumente kommen vor."""
    assert _latest_plan([_call("write_plan", '{"items": [{"id": "1"')]) == []
    assert _latest_plan([_call("write_plan", None)]) == []


def test_fallback_takes_the_last_assistant_turn_not_the_last_message():
    """Die Historie führt auch `cron`/`notification`; die letzte Nachricht zu nehmen
    würde das Panel leeren, sobald eine Benachrichtigung eintrifft."""
    plan = [_call("write_plan", {"items": [{"id": "1", "content": "a", "status": "pending"}]})]
    messages = [
        {"role": "assistant", "tool_activity": plan},
        {"role": "notification", "content": "etwas passierte"},
    ]
    assert _latest_plan(_fallback_entries(messages))[0]["content"] == "a"


def test_fallback_is_empty_when_no_assistant_turn_has_activity():
    assert _fallback_entries([{"role": "user", "content": "hallo"}]) == ()
    assert _fallback_entries([]) == ()
