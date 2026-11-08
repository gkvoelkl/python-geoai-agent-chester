"""The guards of chester-code: corrections appended to a `run_code` result.

A correction in the tool result reaches the model where it cannot skip it — the
PlanGuard device. Two defects, both measured with real models on 2026-10-24:

* a program that ran **one** tool — the model walking the task step by step
  (:data:`ONE_PROGRAM_NOTE`, once per run);
* a program that **ran nothing** — qwen3.6-coder wrapped the whole chain in
  `async def main()` and never called it, three times in a row; each came back as `{}`
  (:data:`DEFINED_NOT_RUN_NOTE`, every time).

A third guard bounds what a program hands back (:class:`ResultSizeGuardCapability`).
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolReturn

_RUN_CODE = "run_code"

#: Fingerprint of the correction, so a test and a reader can find it.
ONE_PROGRAM_NOTE = (
    "This program ran only one tool. Chester Code solves a task with ONE program: write "
    "all remaining steps of the task — data, clipping, computation, map — into your next "
    "`run_code` program, passing each result's \"output\" path to the next call, and end "
    "it with a dict of paths and numbers."
)


DEFINED_NOT_RUN_NOTE = (
    "This program only defined functions and never called them, so no step ran and no "
    "file was written. Write the steps at top level — top-level `await` works — and end "
    "with the result dict; do not wrap them in `async def main()`."
)


def defined_but_never_run(code: str) -> bool:
    """True when the program's top level holds only definitions and imports, no call."""
    try:
        body = ast.parse(code).body
    except SyntaxError:
        return False  # the sandbox reports that itself
    defs = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)
    has_def = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in body)
    runs = [n for n in body if not isinstance(n, defs)
            and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    return has_def and not any(isinstance(sub, (ast.Call, ast.Await))
                               for n in runs for sub in ast.walk(n))


def _with_note(result, note: str):
    if isinstance(result, ToolReturn):
        return ToolReturn(return_value={"result": result.return_value, "note": note},
                          metadata=result.metadata)
    return {"result": result, "note": note}


@dataclass
class OneProgramGuardCapability(AbstractCapability[Any]):
    """Appends :data:`ONE_PROGRAM_NOTE` to the first `run_code` that ran a single tool.

    Nested calls carry the `run_code` call id as prefix (`<id>__1`, `<id>__2`, …; checked
    2026-10-24), and their hooks fire before the `run_code` hook — so the count is
    complete when the program's own result arrives.
    """

    _nested: dict[str, int] = field(default_factory=dict, repr=False)
    _corrected: set[str] = field(default_factory=set, repr=False)

    def get_instructions(self):
        return None  # the correction arrives as a tool result, where it cannot be skipped

    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        call_id = str(getattr(call, "tool_call_id", "") or "")
        if tool_def.name != _RUN_CODE:
            parent, sep, _n = call_id.rpartition("__")
            if sep:
                self._nested[parent] = self._nested.get(parent, 0) + 1
            return result
        code = args.get("code", "") if isinstance(args, dict) else ""
        if isinstance(code, str) and defined_but_never_run(code):
            self._nested.pop(call_id, None)
            return _with_note(result, DEFINED_NOT_RUN_NOTE)
        run = str(getattr(ctx, "run_id", None) or id(ctx.deps))
        if self._nested.pop(call_id, 0) != 1 or run in self._corrected:
            return result
        self._corrected.add(run)
        return _with_note(result, ONE_PROGRAM_NOTE)


#: Characters of a `run_code` result the model gets unchanged. A result of paths and
#: numbers — what the frame asks for — is far below it (the buffer run: under 400).
MAX_RESULT_CHARS = 12_000
#: A value of a result dict above this size is replaced by a stub naming what it was.
MAX_VALUE_CHARS = 1_000
#: Characters of a non-dict result kept as a preview when it is too large.
_PREVIEW_CHARS = 2_000

LARGE_RESULT_NOTE = (
    "This program returned {chars} characters — too much to keep in your context, so the "
    "large values were left out. End a program with a dict of output paths, counts and the "
    "few numbers the answer needs; a whole layer stays in its file and is passed on by path."
)


def _text(value) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _stub(value) -> str:
    items = f" of {len(value)} items" if isinstance(value, (list, tuple, dict, set)) else ""
    return f"<left out: {type(value).__name__}{items}, {len(_text(value))} characters>"


def shrink_result(value) -> tuple[Any, int] | None:
    """``None`` for a result that fits; else the reduced result and the original size.

    A dict keeps its keys and its small values — the paths and numbers the answer needs
    — and only its large values become stubs; anything else becomes a head preview.
    """
    chars = len(_text(value))
    if chars <= MAX_RESULT_CHARS:
        return None
    if isinstance(value, dict):
        kept = {k: v if len(_text(v)) <= MAX_VALUE_CHARS else _stub(v) for k, v in value.items()}
        if len(_text(kept)) <= MAX_RESULT_CHARS:
            return kept, chars
    return {"preview": _text(value)[:_PREVIEW_CHARS] + " …"}, chars


@dataclass
class ResultSizeGuardCapability(AbstractCapability[Any]):
    """Shrinks a `run_code` result too large for the context, and says why.

    Stands in for `ToolOutputLimits`, which the coder drops (its hook would also reduce
    the nested calls a program consumes). Not `ToolOutputLimits(tool_filter=["run_code"])`
    either: it always registers `read_tool_result`, which `CodeMode(tools="all")` would put
    into `run_code`, and its head-and-tail clamp cuts the middle of a result dict — where
    the output paths are. Only the model's view shrinks: the nested results stay in the
    metadata, so the gate (`selmakit.tool_returns`) sees every path.
    """

    def get_instructions(self):
        return None  # the frame already asks for paths and numbers; this is the backstop

    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        if tool_def.name != _RUN_CODE:
            return result
        value = result.return_value if isinstance(result, ToolReturn) else result
        shrunk = shrink_result(value)
        if shrunk is None:
            return result
        kept, chars = shrunk
        note = LARGE_RESULT_NOTE.format(chars=chars)
        if isinstance(result, ToolReturn):
            return ToolReturn(return_value={"result": kept, "note": note},
                              metadata=result.metadata)
        return {"result": kept, "note": note}
