"""chester-code: the coder's tool surface, its instructions, and the one-program guard.

Test-Level 1 — no model, no network. Runs use pydantic-ai's `FunctionModel`, whose
"answers" are fixed `run_code` programs; the surface is read off a `TestModel` request.
"""

from __future__ import annotations

import ast
import inspect

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness.code_mode import CodeMode
from pydantic_ai_harness.planning import Planning
from pydantic_ai_harness.tool_output_limits import ToolOutputLimits

from chester.code import coder
from chester.code.guards import ResultSizeGuardCapability
from chester.code.programcheck import ProgramCheckCapability
from chester.runtime.codemode import wrapper_tool_names
from chester.runtime.planguard import PlanGuardCapability


def _surface():
    model = TestModel(call_tools=[])
    result = Agent(model, capabilities=coder.code_capabilities(planning=False)).run_sync("hi")
    return model.last_model_request_parameters.function_tools, result


def test_run_code_is_the_only_tool():
    tools, _ = _surface()
    assert [t.name for t in tools] == ["run_code"]


def test_every_geo_tool_is_a_function_inside_run_code():
    tools, _ = _surface()
    description = tools[0].description
    expected = {t.__name__ for t in coder.code_tools()}
    assert {"geo_python_run", "inspect_map", "osm_features", "vector_clip"} <= expected
    missing = sorted(n for n in expected if f"async def {n}(" not in description)
    assert not missing, f"not callable inside run_code: {missing}"


def test_the_mcp_adapters_own_hulls_stay_out():
    """`validate_result` duplicated the gate and misread a map PNG (2026-10-24)."""
    assert coder.mcp_only_modules() == {"gatetools", "artifacttools"}
    names = {t.__name__ for t in coder.code_tools()}
    assert "validate_result" not in names
    assert names < wrapper_tool_names() | {"geo_python_run", "inspect_map"}


def test_the_frame_opens_the_instructions():
    _, result = _surface()
    instructions = result.all_messages()[0].instructions or ""
    assert instructions.startswith("## You are Chester Code")


def test_planning_and_output_offloading_are_left_out():
    caps = coder.code_capabilities()
    assert not any(isinstance(c, (Planning, PlanGuardCapability, ToolOutputLimits)) for c in caps)
    assert any(isinstance(c, CodeMode) for c in caps)
    assert any(isinstance(c, coder.OneProgramGuardCapability) for c in caps)
    assert any(isinstance(c, ResultSizeGuardCapability) for c in caps)
    assert any(isinstance(c, ProgramCheckCapability) for c in caps)


def _example_program() -> str:
    block = coder._INSTRUCTIONS.split("```python\n")[1].split("```")[0]
    return block


def test_the_example_program_calls_real_tools_with_real_parameters():
    """The instructions teach by example; a wrong parameter name there is taught too.

    The first CodeMode example passed `path=` to `vector_reproject` (it is
    `input_path`) — found 2026-10-24 before a model saw it. Checked against the real
    signatures, not a stub, so a renamed parameter fails here.
    """
    real = {t.__name__: set(inspect.signature(t).parameters) for t in coder.code_tools()}
    tree = ast.parse(_example_program())
    calls = [n.value for n in ast.walk(tree) if isinstance(n, ast.Await)]
    assert len(calls) >= 5, "the example should show a whole chain, not one step"
    wrong = []
    for call in calls:
        name = call.func.id
        unknown = {k.arg for k in call.keywords} - real.get(name, set())
        if name not in real or unknown:
            wrong.append((name, sorted(unknown)))
    assert not wrong, f"example calls that do not match the tools: {wrong}"


def _run(programs: list[str]) -> list:
    """Run fixed `run_code` programs against two stub tools; return the model's view."""
    seen: list = []
    turns = iter([*(ModelResponse(parts=[ToolCallPart("run_code", {"code": c},
                                                      tool_call_id=f"rc{i}")])
                    for i, c in enumerate(programs)),
                  ModelResponse(parts=[TextPart("done")])])

    def model(messages, _info):
        seen.extend(p.content for p in messages[-1].parts if p.part_kind == "tool-return")
        return next(turns)

    agent = Agent(FunctionModel(model), capabilities=[
        CodeMode(tools=["step_a", "step_b"]), coder.OneProgramGuardCapability()])

    @agent.tool_plain
    def step_a(x: int) -> dict:
        """Stub."""
        return {"ok": True, "output": f"/ws/a{x}.gpkg"}

    @agent.tool_plain
    def step_b(path: str) -> dict:
        """Stub."""
        return {"ok": True, "output": path + ".b"}

    agent.run_sync("go")
    return seen


def test_a_single_tool_program_gets_the_correction_once():
    seen = _run(["await step_a(x=1)", "await step_a(x=2)"])
    notes = [s for s in seen if isinstance(s, dict) and s.get("note") == coder.ONE_PROGRAM_NOTE]
    assert len(notes) == 1
    assert notes[0]["result"] == {"ok": True, "output": "/ws/a1.gpkg"}


def test_a_chained_program_is_left_alone():
    seen = _run(['a = await step_a(x=1)\nawait step_b(path=a["output"])'])
    assert seen == [{"ok": True, "output": "/ws/a1.gpkg.b"}]


_PROGRAM_IN_TEXT = ('Hier ist das Programm:\n```python\n'
                    'r = await step_a(x=1)\nr\n```')


def _answers(responses: list[ModelResponse]) -> tuple[list, str]:
    """Run fixed responses under the unrun-program check; return retries and output."""
    retries: list = []
    turns = iter(responses)

    def model(messages, _info):
        retries.extend(p.content for p in messages[-1].parts if p.part_kind == "retry-prompt")
        return next(turns)

    agent = Agent(FunctionModel(model), capabilities=[CodeMode(tools=["step_a"])])
    agent.output_validator(coder.make_unrun_program_check(frozenset({"step_a"})))

    @agent.tool_plain
    def step_a(x: int) -> dict:
        """Stub."""
        return {"ok": True, "output": f"/ws/a{x}.gpkg"}

    return retries, agent.run_sync("go").output


def test_a_program_written_into_the_answer_is_sent_back_to_be_run():
    retries, output = _answers([
        ModelResponse(parts=[TextPart(_PROGRAM_IN_TEXT)]),
        ModelResponse(parts=[ToolCallPart("run_code", {"code": "await step_a(x=1)"})]),
        ModelResponse(parts=[TextPart("Fertig: /ws/a1.gpkg")]),
    ])
    assert retries == [coder.UNRUN_PROGRAM_NOTE]
    assert output == "Fertig: /ws/a1.gpkg"


def test_showing_the_program_after_running_it_is_fine():
    retries, _ = _answers([
        ModelResponse(parts=[ToolCallPart("run_code", {"code": "await step_a(x=1)"})]),
        ModelResponse(parts=[TextPart(_PROGRAM_IN_TEXT)]),
    ])
    assert retries == []


def test_the_gateway_offers_the_model_nothing_but_run_code(tmp_path):
    """Strict CodeMode: the model has `run_code` and nothing else — no memory, no web.

    Read with a wrapper around a real OpenAI-compatible model, so the native-or-local
    decision of SelmaKit's web tools is made on a real profile: with the capabilities
    left in, `duckduckgo_search` and `web_fetch` stood beside `run_code` (2026-10-24).
    """
    import json as _json

    from pydantic_ai.models.wrapper import WrapperModel
    from selmakit.config import build_model, load_config

    (tmp_path / "chester.json").write_text(_json.dumps(
        {"model": {"model": "local/test", "base_url": "http://127.0.0.1:9/v1"}}))
    seen: dict = {}

    class Recorder(WrapperModel):
        async def request(self, messages, model_settings, params):
            seen["tools"] = sorted(t.name for t in params.function_tools)
            seen["native"] = list(getattr(params, "native_tools", None) or [])
            seen["run_code"] = next(t.description for t in params.function_tools
                                    if t.name == "run_code")
            return ModelResponse(parts=[TextPart("ok")], model_name="recorder")

    inner = coder.build_code_gateway("chester.json", str(tmp_path)).agent._agent
    real = build_model(load_config(str(tmp_path), "chester.json").model)
    with inner.override(model=Recorder(real)):
        inner.run_sync("hi")
    assert seen["tools"] == ["run_code"] and seen["native"] == []
    for name in ("memory_search", "duckduckgo_search", "web_fetch", "osm_features"):
        assert f"async def {name}(" in seen["run_code"], name


def test_the_coder_runs_on_its_own_port_without_telegram(tmp_path):
    """Agent (8000), team (8100) and coder (8200) must be able to run side by side."""
    import json as _json

    (tmp_path / "chester.json").write_text(_json.dumps(
        {"channels": {"webchat": {"port": 8000}, "telegram": {"enabled": True}},
         "code": {"dashboard_port": 9701}}))
    name = coder.effective_config("chester.json", str(tmp_path))
    derived = _json.loads((tmp_path / name).read_text())
    assert name == "chester.code.json"
    assert derived["channels"]["webchat"]["port"] == coder.DEFAULT_WEBCHAT_PORT == 8200
    assert derived["channels"]["telegram"]["enabled"] is False
    block = coder.code_block(_json.loads((tmp_path / "chester.json").read_text()))
    assert block == {"webchat_port": 8200, "dashboard_port": 9701, "bench_port": 8702}


def test_run_code_gets_more_than_three_tries():
    """CodeMode's default of 3 killed a run that was fixing its errors (2026-10-24)."""
    mode = next(c for c in coder.code_capabilities() if isinstance(c, CodeMode))
    assert mode.max_retries == coder.RUN_CODE_RETRIES >= 6


def test_a_large_result_keeps_its_paths_and_loses_its_layer():
    """Without `ToolOutputLimits`, a program returning a whole layer would fill the context."""
    from chester.code import guards

    layer = [{"id": i, "geometry": "POLYGON ((0 0, 1 0, 1 1, 0 0))"} for i in range(2_000)]
    kept, chars = guards.shrink_result({"output": "/ws/b.gpkg", "features": 2_000,
                                        "rows": layer})
    assert chars > guards.MAX_RESULT_CHARS
    assert kept["output"] == "/ws/b.gpkg" and kept["features"] == 2_000
    assert kept["rows"].startswith("<left out: list of 2000 items")
    assert guards.shrink_result({"output": "/ws/b.gpkg", "features": 3}) is None
    preview, _ = guards.shrink_result("x" * 50_000)
    assert len(preview["preview"]) < 3_000


def test_the_size_note_reaches_the_model_and_the_gate_keeps_the_nested_paths():
    from selmakit import tool_returns

    from chester.code import guards

    nested: list = []
    turns = iter([ModelResponse(parts=[ToolCallPart(
        "run_code", {"code": 'a = await step_a(x=1)\n{"out": a["output"], "big": "y" * 20000}'},
        tool_call_id="rc0")]), ModelResponse(parts=[TextPart("done")])])
    seen: list = []

    def model(messages, _info):
        seen.extend(p.content for p in messages[-1].parts if p.part_kind == "tool-return")
        return next(turns)

    agent = Agent(FunctionModel(model), capabilities=[
        CodeMode(tools=["step_a"]), guards.ResultSizeGuardCapability()])

    @agent.tool_plain
    def step_a(x: int) -> dict:
        """Stub."""
        return {"ok": True, "output": f"/ws/a{x}.gpkg"}

    @agent.output_validator
    def collect(ctx, output):
        nested.extend(tool_returns(ctx))
        return output

    agent.run_sync("go")
    assert seen[0]["note"].startswith("This program returned")
    assert seen[0]["result"]["out"] == "/ws/a1.gpkg"
    assert ("step_a", {"ok": True, "output": "/ws/a1.gpkg"}) in nested
