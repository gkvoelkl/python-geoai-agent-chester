"""The gate against the plan loop.

Occasion (2026-09-04, first dashboard run with `Planning`): nine `write_plan` calls in a
row, submissions 2-9 byte-identical, not a single tool call in between — then generation
tipped into a row of hyphens.

The cause is not inertia but feedback: `PlanningToolset.write_plan` checks duplicates,
status and hierarchy, but never whether the plan has **changed**. An identical
resubmission is stored and acknowledged with "Plan updated" — a success message for
having done nothing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from chester.runtime.planguard import PlanGuardCapability

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
    """The measured case: the same plan again, with no work in between."""
    cap = PlanGuardCapability()
    plan = [_item("1", "A", "completed"), _item("2", "B", "in_progress")]
    _run(cap, plan)
    out = _run(cap, plan)
    assert out != _OK
    assert "identical" in out
    assert "step 'B'" in out, "die Korrektur muss den auszuführenden Schritt benennen"


def test_a_real_status_change_passes_through():
    """The counter-check — a gate that blocks real progress would be worse."""
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
    """Two parallel sessions must not slow each other down."""
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
    """Measured 2026-09-07 (`swiss-terrain-slope-grindelwald`): 39 identical calls.

    The guard was right 39 times and ignored 39 times — with the same sentence. A message
    that never changes is no signal after the second time. From the second repetition on
    the count stands in it, and with it a way out.
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
    """Whoever carries on working does not start with a reprimand."""
    cap = PlanGuardCapability()
    plan = [_item("1", "A", "completed"), _item("2", "B", "in_progress")]
    _run(cap, plan)
    _run(cap, plan)
    assert "2nd identical" in _run(cap, plan)

    moved = [_item("1", "A", "completed"), _item("2", "B", "completed")]
    assert _run(cap, moved) == _OK, "eine echte Änderung wird durchgereicht"

    # The first repetition of the *new* plan is the mild form again.
    again = _run(cap, moved)
    assert "Plan NOT updated" in again
    assert "identical write_plan in a row" not in again
