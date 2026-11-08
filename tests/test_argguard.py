"""NoneArgGuardCapability: the string "None" in an optional parameter becomes a real null.

Test-Level 1 — no model; `FunctionModel` sends fixed tool calls, once directly and once
from inside a `run_code` program (chester-code), since both must be corrected.
"""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.code_mode import CodeMode

from chester.runtime.argguard import NoneArgGuardCapability, nullable_params, nulled


def _agent(calls: list, *, code_mode: bool = False):
    received: list = []
    turns = iter([*(ModelResponse(parts=[c]) for c in calls),
                  ModelResponse(parts=[TextPart("ok")])])

    def model(_messages, _info):
        return next(turns)

    caps = [NoneArgGuardCapability(), *([CodeMode(tools=["render"])] if code_mode else [])]
    agent = Agent(FunctionModel(model), capabilities=caps)

    @agent.tool_plain
    def render(output_path: str, column: str | None = None, basemap: str = "OSM") -> dict:
        """Stub of render_map: one nullable parameter, one plain string."""
        received.append({"column": column, "basemap": basemap})
        return {"ok": True}

    agent.run_sync("go")
    return received


def test_a_null_word_in_a_nullable_parameter_becomes_none():
    got = _agent([ToolCallPart("render", {"output_path": "m.html", "column": "None",
                                          "basemap": "None"})])
    assert got == [{"column": None, "basemap": "None"}]  # plain `str` keeps the word


def test_raw_json_arguments_are_corrected_too():
    got = _agent([ToolCallPart("render", '{"output_path": "m.html", "column": "null"}')])
    assert got == [{"column": None, "basemap": "OSM"}]


def test_a_real_value_is_left_alone():
    got = _agent([ToolCallPart("render", {"output_path": "m.html", "column": "height"})])
    assert got == [{"column": "height", "basemap": "OSM"}]


def test_a_call_from_inside_run_code_is_corrected():
    program = 'await render(output_path="m.html", column="None")'
    got = _agent([ToolCallPart("run_code", {"code": program})], code_mode=True)
    assert got == [{"column": None, "basemap": "OSM"}]


def test_nullable_params_reads_the_schema_shapes():
    schema = {"properties": {
        "a": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "b": {"type": ["string", "null"]},
        "c": {"type": "string"},
    }}
    assert nullable_params(schema) == {"a", "b"}
    assert nulled({"a": " none ", "c": "None"}, frozenset({"a"})) == {"a": None, "c": "None"}
