"""RunLogCapability — an append-only trace of every tool call, written as it happens.

**No tools, no instructions.** This capability adds nothing to the prompt; it only
observes. That is the point: it must be free to leave switched on.

Why it exists. A run over the dashboard leaves no readable record until it finishes,
because SelmaKit persists the session at the *end* of a turn. So a long turn is
opaque while it matters most — "where is it now?" can only be answered from CPU time
— and a turn that dies (kill, crash, closed browser) leaves nothing at all. That is
not hypothetical: the dialogue turn of 2026-09-03 switched to the right method after
a user complaint, ran `grass:r.watershed`, produced a map, hit the request limit and
vanished. The only account of it was the browser window.

Why not simply persist the session more often. The session file is the *resume*
artifact — the next turn loads its message history from it — so it must always be a
well-formed message sequence; a tool call without its result is not one. It is also
written with a plain `write_bytes` (selmakit ``session.py:89``), so a crash mid-write
truncates it. Saving it continuously would trade a stale session for a corrupt one.

Two different jobs, therefore two files. The session stays a state to resume from;
this is a log to read. Appending a short line is effectively atomic, there is nothing
to overwrite, and a half-written last line costs a reader one entry — nothing else.

All five hooks are ``async`` — the base class declares them as coroutines, and a
synchronous override fails the run with ``object dict can't be used in 'await'
expression`` at the *first tool result*, i.e. after the log already looks healthy.

They are strict pass-throughs (``before`` returns its ``args``, ``after`` its
``result``), and every write is wrapped: an observer that can break the run it
observes is worse than no observer.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability

#: Cap on a single field. Long enough to identify a call (a bbox, an algorithm id,
#: a parameter dict) and short enough that one tool result cannot bury the file —
#: a `vector_info` on an OSM layer runs to several thousand characters.
_MAX_FIELD = 1200

DEFAULT_LOG_DIR = ".chester/logs/runs"


def _short(value: Any) -> str:
    """A value as a single-line string, truncated with the original length kept."""
    try:
        text = (
            value if isinstance(value, str)
            else json.dumps(value, default=str, ensure_ascii=False)
        )
    except (TypeError, ValueError):
        text = repr(value)
    text = " ".join(text.split())
    if len(text) <= _MAX_FIELD:
        return text
    return f"{text[:_MAX_FIELD]}… (+{len(text) - _MAX_FIELD} chars)"


def _safe_name(session_key: Any) -> str:
    """A session key as a filename — the dashboard's keys are hashes, but `/` and
    `:` appear in the CLI ones (`testprompt:<id>`)."""
    text = str(session_key or "session")
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in text)[:80]


@dataclass
class RunLogCapability(AbstractCapability[Any]):
    """Appends one JSONL line per tool call, tool result and model reply, live."""

    log_dir: str = DEFAULT_LOG_DIR
    _started: dict[str, float] = field(default_factory=dict, repr=False)

    def get_instructions(self):
        """Nothing — and that is the design, not an omission.

        Every capability must answer this (the contract is enforced by
        `test_every_capability_implements_the_contract`), because one that offers
        tools must explain them. This one offers none: it observes the run and the
        model has no business knowing it is watched. Telling it would cost prompt
        tokens and invite it to write *for* the log.
        """
        return None

    # ── writing ─────────────────────────────────────────────────────────

    def _write(self, session_key: Any, record: dict) -> None:
        """Append one line. Never raises — a log must not be able to fail a run."""
        try:
            directory = Path(self.log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            record = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            # Open per line and let the OS append: two writers (gateway and a CLI
            # run sharing a session key) then interleave whole lines instead of
            # overwriting each other, and nothing is held open across a long tool.
            with open(directory / f"{_safe_name(session_key)}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            pass

    # ── hooks ───────────────────────────────────────────────────────────

    async def after_model_request(self, ctx: RunContext[Any], *, request_context, response):
        """Record what the model *said*, not only what it called.

        Added 2026-09-04 after the third text-level failure of the day went
        unrecorded: a turn that had finished a map degenerated into repeating the
        link endlessly, and — because the session is only written when a turn ends —
        aborting it left no trace at all. Tool calls alone cannot show that: this run
        made 21 of them and every one succeeded.

        `repeats` is the point of the entry. It counts the most frequent non-empty
        line, so a degenerate reply is visible as a number instead of needing the
        full text: a healthy answer sits at 1-2, the loop above would have been in
        the hundreds. Ollama has no `max_tokens` here and SelmaKit's ModelConfig
        exposes none, so nothing stops such a generation on its own.
        """
        parts = [
            getattr(p, "content", "")
            for p in getattr(response, "parts", [])
            if getattr(p, "part_kind", "") == "text"
        ]
        text = "".join(x for x in parts if isinstance(x, str)).strip()
        if not text:
            return response
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        repeats = max(Counter(lines).values()) if lines else 0
        self._write(
            ctx.deps,
            {"kind": "text", "chars": len(text), "repeats": repeats, "text": _short(text)},
        )
        return response

    async def on_tool_validate_error(self, ctx: RunContext[Any], *, call, tool_def, args, error):
        """Ein Aufruf, der schon an der Argumentprüfung scheitert.

        Die blinde Stelle, die am 2026-09-05 ein ganzes Protokoll gekostet hat:
        `before_tool_execute` feuert erst **nach** der Validierung. War der erste
        Aufruf eines Laufs fehlerhaft — hier `write_plan` mit `"id": 1`, wo eine
        Zeichenkette verlangt ist —, wurde die Protokolldatei gar nicht erst
        angelegt, und der Lauf sah von außen aus, als hätte er kein Werkzeug
        benutzt. Genau falsch: Er hatte es versucht.

        **Muss weiterwerfen.** Der Kontrakt ist derselbe wie bei
        `on_tool_execute_error`: Ein Rückgabewert würde als geprüfte Argumente
        gelten und den fehlerhaften Aufruf ausführen.
        """
        self._write(
            ctx.deps,
            {
                "kind": "invalid",
                "tool": getattr(tool_def, "name", "?"),
                "args": _short(args),
                "error": _short(str(error)),
            },
        )
        raise error

    async def before_tool_execute(self, ctx: RunContext[Any], *, call, tool_def, args):
        """Record that a call *started* — so a hanging tool is visible as such."""
        self._started[getattr(call, "tool_call_id", "") or ""] = time.monotonic()
        self._write(ctx.deps, {"kind": "call", "tool": tool_def.name, "args": _short(args)})
        return args

    async def after_tool_execute(self, ctx: RunContext[Any], *, call, tool_def, args, result):
        self._write(
            ctx.deps,
            {
                "kind": "result",
                "tool": tool_def.name,
                "seconds": self._elapsed(call),
                "result": _short(result),
            },
        )
        return result

    async def on_tool_execute_error(self, ctx: RunContext[Any], *, call, tool_def, args, error):
        """A raising tool never reaches `after_tool_execute` — without this, the log
        would show a call that simply stops, indistinguishable from a hang.

        **Must re-raise.** The contract is "return any value to suppress the error
        and use it as the tool result": returning ``None`` here would silently
        swallow every tool failure and hand the model ``None`` instead. Logging an
        error is an observation; deciding what happens to it is not this
        capability's business.
        """
        self._write(
            ctx.deps,
            {
                "kind": "error",
                "tool": tool_def.name,
                "seconds": self._elapsed(call),
                "error": _short(f"{type(error).__name__}: {error}"),
            },
        )
        raise error

    def _elapsed(self, call) -> float | None:
        started = self._started.pop(getattr(call, "tool_call_id", "") or "", None)
        return round(time.monotonic() - started, 2) if started else None
