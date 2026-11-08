"""What every Chester agent needs to live with CodeMode's ``run_code`` — shared, not wired.

CodeMode (pydantic-ai-harness) replaces single tool calls with one ``run_code`` tool in
which the model writes a Python program that calls tools as functions. It is the whole
architecture of chester-code (``chester.code``); chester-agent does not use it (tried and
taken out again on 2026-10-24, ``doc/codemode-concept.md``). Two things stay here
because more than one reader needs them:

* :func:`flatten_code_mode` / :func:`load_flat_session` — ``selmakit.tool_returns``
  unpacks the nested calls for the gate, but a reader that walks the persisted session
  itself (``testprompt.py``, ``trace.py``) sees only ``run_code``: one call where six
  tools ran, and a tool coverage of zero.
* :func:`misrouted_tool_program` — the correction ``geo_python_run`` gives a
  tool-calling program that was sent to it instead of to ``run_code``.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any

from selmakit import load_session_messages


@lru_cache(maxsize=1)
def wrapper_tool_names() -> frozenset[str]:
    """Every tool name of the wrapper layer — derived, never a hand-kept list."""
    from chester import wrapperlayer

    return frozenset(t.__name__ for t in wrapperlayer.collect_tools("/tmp/chester-tool-probe"))


_AWAITED_CALL = re.compile(r"\bawait\s+([a-z_][a-z0-9_]*)\s*\(")


def misrouted_tool_program(code: str) -> dict | None:
    """The correction for a tool-calling program sent to ``geo_python_run``, else ``None``.

    Measured 2026-10-24, the first CodeMode run (`buffer-schools-500m`): gemma4 wrote a
    correct `run_code` program — `await geocode(...)`, `await osm_features(...)`, checks
    on `ok` — and sent it to `geo_python_run`, where no tool exists. It failed, the geo
    tools were nowhere in the native catalogue, and the run spent its remaining eleven
    minutes on 40 web searches until the 64k context overflowed. Such a program can
    never run there, so this refuses **every** time, not once like the hand-rolled guard,
    and names both ways out: `run_code` when it is offered, direct calls otherwise.
    """
    called = sorted({name for name in _AWAITED_CALL.findall(code or "")
                     if name in wrapper_tool_names()})
    if not called:
        return None
    names = ", ".join(f"`{n}`" for n in called)
    return {
        "ok": False,
        "error": (f"This program calls Chester tools ({names}); they are not callable "
                  "inside `geo_python_run`, whose namespace is geopandas only. If a "
                  "`run_code` tool is offered, send this same program to `run_code`. "
                  "Otherwise call the tools directly, one call per step."),
        "tools_called": called,
    }


def _nested_parts(metadata: Any) -> list[dict]:
    """The nested call/return part dicts of one serialized ``run_code`` return, in call order."""
    if not isinstance(metadata, dict) or not metadata.get("code_mode"):
        return []
    calls = metadata.get("tool_calls")
    returns = metadata.get("tool_returns")
    calls = calls if isinstance(calls, dict) else {}
    returns = returns if isinstance(returns, dict) else {}
    out: list[dict] = []
    for call_id, call in calls.items():
        if isinstance(call, dict):
            out.append(call)
        ret = returns.get(call_id)
        if isinstance(ret, dict):
            out.append(ret)
    return out


def flatten_code_mode(messages: list[dict]) -> list[dict]:
    """Persisted messages with each ``run_code``'s nested calls spliced in before its return.

    Messages without CodeMode come back unchanged. The guards are ``isinstance`` checks
    on purpose — the metadata shape crosses the version boundary to the harness, and a
    change there should cost the nested calls, not crash a reader.
    """
    out: list[dict] = []
    for msg in messages:
        parts = msg.get("parts") if isinstance(msg, dict) else None
        if not isinstance(parts, list):
            out.append(msg)
            continue
        new_parts: list = []
        for part in parts:
            if isinstance(part, dict) and part.get("part_kind") == "tool-return":
                new_parts.extend(_nested_parts(part.get("metadata")))
            new_parts.append(part)
        out.append({**msg, "parts": new_parts})
    return out


def load_flat_session(sessions_dir: str | os.PathLike[str], session_key: str) -> list[dict]:
    """`selmakit.load_session_messages`, then :func:`flatten_code_mode` — one call for readers."""
    return flatten_code_mode(load_session_messages(sessions_dir, session_key))
