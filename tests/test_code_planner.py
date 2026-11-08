"""chester-code's requirement step and source handbooks (CC.4).

Test-Level 1 — no model, no network. A `FunctionModel` answers the planner's output tool
with a fixed requirement and then records what the coder's first request carried.
"""

from __future__ import annotations

import ast
import inspect

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from chester.code import coder, handbooks, planner

_PLAN = {
    "goal": "mean elevation per city district of Regensburg",
    "area": "Regensburg, clipped to the city boundary",
    "layers": [{"layer": "city districts", "source": "city_districts"},
               {"layer": "elevation", "source": "terrain"}],
    "crs": "EPSG:25832",
    "method": "zonal mean of the DEM per district",
    "output": "districts with mean elevation, a map",
}


def _run(plan: dict | None):
    seen: dict = {}

    def model(messages, info):
        if info.output_tools and not info.function_tools:  # the requirement step
            if plan is None:
                raise RuntimeError("planner down")
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, plan)])
        seen["tools"] = info.function_tools
        seen["instructions"] = info.instructions or ""
        return ModelResponse(parts=[TextPart("done")])

    Agent(FunctionModel(model), capabilities=coder.code_capabilities()).run_sync(
        "Mittlere Geländehöhe je Stadtbezirk in Regensburg")
    return seen


def test_the_requirement_and_its_handbooks_reach_the_coder():
    seen = _run(_PLAN)
    text = seen["instructions"]
    assert text.startswith("## You are Chester Code")
    assert "## The requirement for this task" in text
    assert "city districts ← `city_districts`" in text
    assert "### Source `city_districts`" in text and "### Source `terrain`" in text
    assert "### Source `aerial`" not in text


def test_the_unchosen_sources_functions_leave_run_code():
    seen = _run(_PLAN)
    assert [t.name for t in seen["tools"]] == ["run_code"]
    description = seen["tools"][0].description
    for kept in ("wfs_features", "geodata_search", "fetch_dem", "zonal_stats", "render_map",
                 "geocode", "fetch_boundaries", "vector_clip"):
        assert f"async def {kept}(" in description, kept
    for dropped in ("fetch_dop", "stac_search", "fetch_gtfs_stops", "fetch_lod2"):
        assert f"async def {dropped}(" not in description, dropped


def test_a_failed_plan_leaves_the_coder_as_before():
    seen = _run(None)
    assert "## The requirement" not in seen["instructions"]
    assert "async def fetch_dop(" in seen["tools"][0].description


def test_every_source_has_a_handbook_and_every_handbook_a_planner_id():
    ids = {h.id for h in handbooks.HANDBOOKS}
    assert ids | {"none"} == set(planner.SourceId.__args__)
    assert all(f"`{i}`" in planner.PLANNER_INSTRUCTIONS for i in ids)


def test_handbook_functions_exist():
    real = {t.__name__ for t in coder.code_tools()}
    named = {f for h in handbooks.HANDBOOKS for f in h.functions} | handbooks.ALWAYS
    assert named <= real, sorted(named - real)


def test_every_template_calls_real_tools_with_real_parameters():
    """A template is taught as it stands — a wrong parameter name there is taught too."""
    real = {t.__name__: inspect.signature(t) for t in coder.code_tools()}
    for h in handbooks.HANDBOOKS:
        tree = ast.parse("async def _t():\n" + "\n".join(
            "    " + line for line in h.template.splitlines()))
        calls = [n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)]
        assert calls, h.id
        for call in calls:
            name = call.func.id
            assert name in real, (h.id, name)
            sig = real[name]
            kwargs = {k.arg: None for k in call.keywords}
            sig.bind(**kwargs)  # raises on an unknown or a missing parameter


def test_a_program_that_only_defines_main_is_told_to_run_its_steps():
    """Measured 2026-10-24: three `async def main()` programs in a row, each never called."""
    from chester.code import guards

    assert guards.defined_but_never_run(
        'import asyncio\n\nasync def main():\n    r = await step_a(x=1)\n    {"r": r}\n')
    assert not guards.defined_but_never_run(
        "async def main():\n    return await step_a(x=1)\n\nawait main()\n")
    assert not guards.defined_but_never_run("r = await step_a(x=1)\nr\n")
    assert not guards.defined_but_never_run("def f(:\n")


def test_the_never_run_note_reaches_the_model():
    from test_chester_code import _run as run_programs

    from chester.code import guards

    seen = run_programs(["async def main():\n    await step_a(x=1)\n"])
    assert any(isinstance(s, dict) and s.get("note") == guards.DEFINED_NOT_RUN_NOTE
               for s in seen)


def test_the_notes_name_only_real_tools_and_parameters():
    """The notes teach the wrapper modules' traps; a tool or keyword they invent is taught too."""
    import re

    real = {t.__name__: inspect.signature(t) for t in coder.code_tools()}
    texts = [h.notes for h in handbooks.HANDBOOKS] + [handbooks.ANALYSIS_NOTES]
    for text in texts:
        for name, args in re.findall(r"`([a-z_]+)\(([^`]*)\)`", text):
            assert name in real, name
            for kw in re.findall(r"\b([a-z_]+)=", args):
                assert kw in real[name].parameters, (name, kw)


def test_every_template_feed_is_a_real_feed():
    """The GTFS template once fetched `feed="de_regensburg"` — no such feed exists."""
    import re

    from chester import gtfs

    feeds = [(h.id, f) for h in handbooks.HANDBOOKS
             for f in re.findall(r'feed="([^"]+)"', h.template)]
    assert feeds
    assert all(f in gtfs.FEEDS for _id, f in feeds), feeds


def test_the_analysis_facts_reach_every_run_planned_or_not():
    assert handbooks.ANALYSIS_NOTES in _run(_PLAN)["instructions"]
    assert handbooks.ANALYSIS_NOTES in _run(None)["instructions"]
