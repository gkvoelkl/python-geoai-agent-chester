"""Der Riegel gegen die Plan-Schleife.

Anlass (2026-09-04, erster Dashboard-Lauf mit `Planning`): neun `write_plan`-Aufrufe
hintereinander, die Einreichungen 2-9 byte-identisch, dazwischen kein einziger
Werkzeugaufruf — danach kippte die Generierung in eine Reihe von Bindestrichen.

Ursache ist keine Trägheit, sondern Rückkopplung: `PlanningToolset.write_plan` prüft
Dubletten, Status und Hierarchie, aber nie, ob der Plan sich **geändert** hat. Eine
identische Wiedervorlage wird gespeichert und mit „Plan updated" quittiert — eine
Erfolgsmeldung dafür, nichts getan zu haben.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from chester.capabilities.planguard import PlanGuardCapability

_OK = "Plan updated: 2 step(s). 1. [x] A 2. [~] B (1/2 completed)"


def _item(id_, content, status):
    return {"id": id_, "content": content, "status": status}


def _run(cap, items, session="s1", result=_OK):
    return asyncio.run(
        cap.after_tool_execute(
            SimpleNamespace(deps=session),
            call=SimpleNamespace(tool_call_id="c"),
            tool_def=SimpleNamespace(name="write_plan"),
            args={"items": items},
            result=result,
        )
    )


def test_the_first_plan_passes_through():
    plan = [_item("1", "A", "completed"), _item("2", "B", "in_progress")]
    assert _run(PlanGuardCapability(), plan) == _OK


def test_an_identical_second_plan_is_answered_with_a_correction():
    """Der gemessene Fall: derselbe Plan noch einmal, ohne Arbeit dazwischen."""
    cap = PlanGuardCapability()
    plan = [_item("1", "A", "completed"), _item("2", "B", "in_progress")]
    _run(cap, plan)
    out = _run(cap, plan)
    assert out != _OK
    assert "identical" in out
    assert "step 'B'" in out, "die Korrektur muss den auszuführenden Schritt benennen"


def test_a_real_status_change_passes_through():
    """Die Gegenprobe — ein Riegel, der echte Fortschritte blockt, wäre schlimmer."""
    cap = PlanGuardCapability()
    _run(cap, [_item("1", "A", "completed"), _item("2", "B", "in_progress")])
    out = _run(cap, [_item("1", "A", "completed"), _item("2", "B", "completed")])
    assert out == _OK


def test_a_new_step_passes_through():
    cap = PlanGuardCapability()
    plan = [_item("1", "A", "in_progress")]
    _run(cap, plan)
    assert _run(cap, [*plan, _item("2", "B", "pending")]) == _OK


def test_reordering_counts_as_a_change():
    cap = PlanGuardCapability()
    a, b = _item("1", "A", "pending"), _item("2", "B", "pending")
    _run(cap, [a, b])
    assert _run(cap, [b, a]) == _OK


def test_sessions_do_not_share_a_plan():
    """Zwei parallele Sitzungen dürfen sich nicht gegenseitig ausbremsen."""
    cap = PlanGuardCapability()
    plan = [_item("1", "A", "in_progress")]
    _run(cap, plan, session="s1")
    assert _run(cap, plan, session="s2") == _OK


def test_other_tools_are_untouched():
    cap = PlanGuardCapability()
    out = asyncio.run(
        cap.after_tool_execute(
            SimpleNamespace(deps="s1"),
            call=SimpleNamespace(tool_call_id="c"),
            tool_def=SimpleNamespace(name="geocode"),
            args={"query": "Tegernheim"},
            result={"ok": True},
        )
    )
    assert out == {"ok": True}


def test_an_unexpected_argument_shape_disables_the_guard():
    """Lieber wirkungslos als falsch: raten wäre schlimmer als nichts tun."""
    cap = PlanGuardCapability()
    assert _run(cap, "kein Plan") == _OK
    assert _run(cap, "kein Plan") == _OK


def test_the_guard_gets_louder_when_it_is_ignored():
    """Gemessen 2026-09-07 (`swiss-terrain-slope-grindelwald`): 39 identische Aufrufe.

    Der Wächter hatte 39-mal recht und wurde 39-mal überhört — mit demselben Satz.
    Eine Meldung, die sich nicht ändert, ist nach der zweiten kein Signal mehr. Ab der
    zweiten Wiederholung steht die Zahl darin und mit ihr ein Ausweg.
    """
    cap = PlanGuardCapability()
    plan = [_item("1", "A", "completed"), _item("2", "B", "in_progress")]
    assert _run(cap, plan) == _OK

    first = _run(cap, plan)
    assert "Plan NOT updated" in first
    assert "2nd identical" not in first, "beim ersten Mal reicht der Hinweis"

    second = _run(cap, plan)
    assert "2nd identical" in second
    assert "stop" in second, "der Ausweg gehört dazu: aufhören ist erlaubt"

    third = _run(cap, plan)
    assert "3rd identical" in third


def test_a_real_edit_resets_the_count():
    """Wer weiterarbeitet, fängt nicht mit einer Rüge an."""
    cap = PlanGuardCapability()
    plan = [_item("1", "A", "completed"), _item("2", "B", "in_progress")]
    _run(cap, plan)
    _run(cap, plan)
    assert "2nd identical" in _run(cap, plan)

    moved = [_item("1", "A", "completed"), _item("2", "B", "completed")]
    assert _run(cap, moved) == _OK, "eine echte Änderung wird durchgereicht"

    # Die erste Wiederholung des *neuen* Plans ist wieder die milde Form.
    again = _run(cap, moved)
    assert "Plan NOT updated" in again
    assert "identical write_plan in a row" not in again
