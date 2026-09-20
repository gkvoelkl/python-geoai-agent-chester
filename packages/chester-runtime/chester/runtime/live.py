"""A thin channel for live output from inside a tool — used by chester-team.

With the team, what the user watches are the orchestrator's calls: `ressort_vector`
starts, and for minutes nothing else appears, because the real work happens inside
that one call. The ressort agent has no idea a terminal or a bench is watching.

So the runner (``ask.py``) publishes its stream here for the duration of a run, and
whoever runs *inside* it can write a line. A context variable rather than a parameter
threaded through half the code base: the ressort is called by pydantic-ai, not by us,
and the call chain in between is the framework's.

Never fatal, never required: with no sink set, :func:`emit` does nothing, which is the
case for every single-agent run and every test.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_sink: ContextVar[Callable[[str], None] | None] = ContextVar("chester_live_sink", default=None)


@contextmanager
def use_sink(sink: Callable[[str], None] | None) -> Iterator[None]:
    """Publish ``sink`` as the live channel for the duration of the block."""
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def emit(text: str) -> None:
    """Write one line to the live channel, if there is one. Never raises."""
    sink = _sink.get()
    if sink is None:
        return
    try:
        sink(text)
    except Exception:  # noqa: BLE001 - a display must never break the run it shows
        pass


def short(value: object, limit: int) -> str:
    """Tool arguments and results, compact and trimmed — the same shape `ask.py` uses."""
    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}… (+{len(text) - limit})"
