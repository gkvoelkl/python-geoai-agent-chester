"""PlanGuardCapability — stops the plan-rewriting loop by refusing to reward it.

Measured 2026-09-04, the first dashboard run with `Planning` wired: the model called
`write_plan` **nine times in a row**, submissions 2-9 byte-identical (step 1 done,
step 2 `in_progress`), with no tool call in between. After roughly nine minutes of
that the generation collapsed into a run of dashes and the turn was lost.

The mechanism is not laziness, it is feedback. `PlanningToolset.write_plan` validates
duplicate ids, statuses and hierarchy — but never whether the plan actually *changed*.
An identical resubmission is stored and answered with "Plan updated: 6 step(s) …
(0/6 completed)": a **success message for doing nothing**. With `inject=True` the same
plan is then put back in front of the model on the next turn, which makes "the plan
needs updating" the most salient available action, and the loop closes.

So this capability turns that no-op into a correction. Same device the harness uses on
itself (`_ALL_DONE_NOTE`: *"Do NOT call read_plan again"*), and the same one Chester
already uses in `qgis_python` and `vector_filter`: the tool answers with what to do
instead. Prose in the system prompt is a request; a tool result is a fact the model
cannot skip reading.

Deliberately **not** merged into `RunLogCapability`. That one must never influence the
run it records; this one exists to influence it. Keeping an observer and an intervener
in one class is how an observer quietly becomes one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability

_PLAN_TOOL = "write_plan"

#: Ab der wievielten unveränderten Wiederholung der Wächter lauter wird. Gemessen
#: 2026-09-07 (`swiss-terrain-slope-grindelwald`): **39** identische Aufrufe in 16
#: Minuten, jedes Mal mit derselben Antwort. Der Wächter hatte recht und wurde 39-mal
#: überhört — eine Meldung, die sich nicht ändert, ist nach der zweiten kein Signal
#: mehr. Ab hier steht die Zahl in der Antwort und mit ihr ein Ausweg: aufhören ist
#: erlaubt.
_LOUD_AFTER = 2


def _signature(items: Any) -> tuple | None:
    """The plan reduced to what a *change* would alter: ids, texts, statuses.

    Order matters — reordering steps is a real edit. Returns ``None`` when the shape
    is not what we expect, which disables the guard rather than guessing.
    """
    if not isinstance(items, (list, tuple)):
        return None
    signature = []
    for item in items:
        get = item.get if isinstance(item, dict) else lambda k, i=item: getattr(i, k, None)
        status = get("status")
        signature.append((
            str(get("id")),
            str(get("content")),
            str(getattr(status, "value", status)),
        ))
    return tuple(signature)


def _ordinal(n: int) -> str:
    """``2nd``/``3rd``/``11th`` — die Zahl soll lesbar sein, nicht „2th"."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _in_progress(items: Any) -> str | None:
    """The step the plan claims to be working on — the one to name in the nudge."""
    for _, content, status in _signature(items) or ():
        if status == "in_progress":
            return content
    return None


@dataclass
class PlanGuardCapability(AbstractCapability[Any]):
    """Replaces the success message of an unchanged ``write_plan`` with a correction."""

    _last: dict[str, tuple] = field(default_factory=dict, repr=False)
    _repeats: dict[str, int] = field(default_factory=dict, repr=False)

    def get_instructions(self):
        """None — the correction is delivered where it cannot be skipped.

        Putting "do not rewrite an unchanged plan" into the system prompt would add
        the rule to the 42k characters the model already does not follow (measured
        this session: `load_capability` once in 108 sessions; the returned-path rule
        missed in four runs out of four). It arrives as a tool result instead.
        """
        return None

    async def after_tool_execute(self, ctx: RunContext[Any], *, call, tool_def, args, result):
        if tool_def.name != _PLAN_TOOL:
            return result

        items = args.get("items") if isinstance(args, dict) else getattr(args, "items", None)
        signature = _signature(items)
        if signature is None:
            return result

        key = str(ctx.deps)
        unchanged = self._last.get(key) == signature
        self._last[key] = signature
        if not unchanged:
            self._repeats.pop(key, None)
            return result

        n = self._repeats[key] = self._repeats.get(key, 0) + 1
        step = _in_progress(items)
        target = f"step '{step}'" if step else "the next step"
        message = (
            "Plan NOT updated: it is identical to the one you just wrote, so this call "
            "changed nothing. Do not call write_plan again now — carry out "
            f"{target} by calling the tool it needs. Write the plan again only after a "
            "step's status has actually changed."
        )
        if n >= _LOUD_AFTER:
            message += (
                f" This is now the {_ordinal(n)} identical write_plan in a row: {n} "
                "calls, "
                "no progress. The plan is not the work, and rewriting it cannot "
                f"advance it. Either call the tool {target} needs on your next turn, "
                "or — if you do not know which tool that is — say so in your answer "
                "and stop, with whatever result you already have."
            )
        return message
