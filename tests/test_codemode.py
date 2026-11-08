"""The shared CodeMode pieces in chester-runtime: trace unpacking and the misroute guard.

Test-Level 1 — no model, no network. The run uses pydantic-ai's `FunctionModel`, whose
"answers" are fixed `run_code` programs, and is persisted exactly the way SelmaKit
persists a session (`TypeAdapter(list[ModelMessage]).dump_json`). The architecture that
uses CodeMode, chester-code, has its own tests in `tests/test_chester_code.py`.
"""

from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.code_mode import CodeMode

import agent_build
from chester.runtime import codemode


def test_chester_agent_does_not_carry_code_mode():
    """Taken out of the single agent on 2026-10-24 — it lives in chester-code now."""
    caps = agent_build.geo_capabilities()
    assert not any(isinstance(c, CodeMode) for c in caps)


def _persisted_code_mode_run() -> list[dict]:
    """One `run_code` with two nested tools, persisted and read back as plain dicts."""
    turns = iter([
        ModelResponse(parts=[ToolCallPart("run_code", {
            "code": 'a = await step_a(x=1)\nb = await step_b(path=a["output"])\nb'})]),
        ModelResponse(parts=[TextPart("done")]),
    ])
    agent = Agent(FunctionModel(lambda _m, _i: next(turns)),
                  capabilities=[CodeMode(tools=["step_a", "step_b"])])

    @agent.tool_plain
    def step_a(x: int) -> dict:
        """First step."""
        return {"ok": True, "output": f"/ws/a_{x}.gpkg"}

    @agent.tool_plain
    def step_b(path: str) -> dict:
        """Second step."""
        return {"ok": True, "output": path.replace("a_", "b_"), "warning": "bbox used"}

    result = agent.run_sync("go")
    raw = TypeAdapter(list[ModelMessage]).dump_json(result.all_messages())
    return json.loads(raw)


def test_flatten_splices_nested_calls_before_their_run_code_return():
    messages = codemode.flatten_code_mode(_persisted_code_mode_run())
    sequence = [(p["part_kind"], p.get("tool_name"))
                for m in messages for p in m["parts"]
                if p.get("part_kind") in ("tool-call", "tool-return")]
    assert sequence == [
        ("tool-call", "run_code"),
        ("tool-call", "step_a"), ("tool-return", "step_a"),
        ("tool-call", "step_b"), ("tool-return", "step_b"),
        ("tool-return", "run_code"),
    ]
    step_b = next(p for m in messages for p in m["parts"]
                  if p.get("part_kind") == "tool-return" and p.get("tool_name") == "step_b")
    assert step_b["content"]["warning"] == "bbox used"


def test_flatten_leaves_a_plain_session_alone():
    plain = [{"parts": [{"part_kind": "tool-return", "tool_name": "geocode",
                         "content": {"ok": True}, "metadata": None}]}]
    assert codemode.flatten_code_mode(plain) == plain


def test_flatten_survives_a_changed_metadata_shape():
    odd = [{"parts": [{"part_kind": "tool-return", "tool_name": "run_code",
                       "metadata": {"code_mode": True, "tool_calls": ["not", "a", "dict"]}}]}]
    assert codemode.flatten_code_mode(odd) == odd


def test_tool_names_come_from_the_wrapper_layer():
    names = codemode.wrapper_tool_names()
    assert {"geocode", "osm_features", "vector_buffer", "render_map"} <= names
    assert "write_plan" not in names and "geo_python_run" not in names


# The program gemma4 sent to `geo_python_run` in the first CodeMode run (2026-10-24).
_MISROUTED = '''
async def run():
    geo_res = await geocode(query="Regensburg", output_path="regensburg_boundary.gpkg")
    if not geo_res.get("ok"):
        return geo_res
    osm_res = await osm_features(place="Regensburg", tags={"amenity": "school"},
                                 output_path="schools_raw.gpkg")
    return osm_res
'''


def test_a_tool_calling_program_is_redirected_to_run_code():
    refusal = codemode.misrouted_tool_program(_MISROUTED)
    assert refusal["ok"] is False
    assert refusal["tools_called"] == ["geocode", "osm_features"]
    assert "run_code" in refusal["error"]


@pytest.mark.parametrize("code", [
    "gdf = read_vector('a.gpkg')\nresult = len(gdf)",   # a real geopandas snippet
    "await asyncio.sleep(1)",                           # awaits, but no Chester tool
    "slope = 3\nresult = slope * 2",                    # a tool name, not called
])
def test_ordinary_snippets_are_not_redirected(code):
    assert codemode.misrouted_tool_program(code) is None


def test_geo_python_run_refuses_the_program_without_running_it(tmp_path):
    from chester.runtime.geopython import build_geo_python_run

    tool = build_geo_python_run(str(tmp_path))
    out = tool(None, _MISROUTED)
    assert out["ok"] is False and out["tools_called"] == ["geocode", "osm_features"]
    assert not list(tmp_path.rglob("*.gpkg"))   # nothing ran, nothing was written
