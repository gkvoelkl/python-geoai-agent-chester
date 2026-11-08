"""Every program the coder is taught must pass CodeMode's type check.

Test-Level 1 — no model, no network. Since Phase WB the wrapper tools carry typed results,
and CodeMode type-checks each program against those stubs before it runs: a template that
fails the check teaches the coder a program the sandbox refuses. The frame example and
every handbook template run through the real check here, with stub tools that keep the
real signatures and result types; only the type check counts — the stubs return nothing
useful, so a runtime error afterwards is expected and ignored.
"""

from __future__ import annotations

import ast
import inspect

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.code_mode import CodeMode

from chester import toolreturns, wrapperlayer
from chester.code import coder, handbooks

_REAL = {t.__name__: t for t in wrapperlayer.collect_tools("/tmp/chester-template-types")}


def _stub(name: str):
    real = _REAL[name]

    def stub(*_a, **_k):
        return {"ok": True}

    stub.__name__ = name
    stub.__doc__ = "Stub."
    stub.__signature__ = inspect.signature(real)  # type: ignore[attr-defined]
    stub.__annotations__ = dict(real.__annotations__)
    return toolreturns.typed(stub)


def _type_error(program: str) -> str | None:
    """CodeMode's type-check verdict on ``program``: the message, or None when it passes."""
    names = sorted({n.value.func.id for n in ast.walk(ast.parse(program))  # type: ignore[attr-defined]
                    if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
                    and isinstance(n.value.func, ast.Name) and n.value.func.id in _REAL})
    seen: list = []
    turns = iter([ModelResponse(parts=[ToolCallPart("run_code", {"code": program})]),
                  ModelResponse(parts=[TextPart("done")])])

    def model(messages, _info):
        seen.extend(str(p.content) for p in messages[-1].parts
                    if p.part_kind in ("tool-return", "retry-prompt"))
        return next(turns)

    agent = Agent(FunctionModel(model), capabilities=[CodeMode(tools=names)])
    for name in names:
        agent.tool_plain(_stub(name))
    agent.run_sync("go")
    return seen[0] if seen and "Type error" in seen[0] else None


def test_the_frame_example_type_checks():
    example = coder._INSTRUCTIONS.split("```python\n")[1].split("```")[0]
    assert _type_error(example) is None


@pytest.mark.parametrize("book", handbooks.HANDBOOKS, ids=lambda h: h.id)
def test_every_handbook_template_type_checks(book):
    assert _type_error(book.template) is None


def test_the_check_would_catch_a_badly_typed_template():
    """The harness itself: a count used as a list must fail."""
    bad = ('s = await osm_features(place="X", tags={"a": "b"}, output_path="s.gpkg")\n'
           'n = len(s["features"])')
    assert _type_error(bad) is not None
