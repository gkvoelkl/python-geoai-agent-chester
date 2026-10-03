"""Test-Level 1: the ressort agents of chester-team, without a language model.

A `FunctionModel` scripts the model's side — one tool call, then the handover — so the
wrapper itself is under test: which tools a ressort gets, what it is told, that the
produced paths come back even when the model forgets to list them, that a cap says it
capped, and that every call leaves a record. The real model run is Test-Level 2+.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import geopandas as gpd
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from shapely.geometry import Point

from chester import ressortcut
from chester.team import ressorts

GEODATA: dict = {"roots": [], "postgis": None, "stac_catalogs": None, "ttl_by_source": {}}


def _workspace(tmp_path: Path) -> str:
    cache = tmp_path / "geocache"
    cache.mkdir()
    gpd.GeoDataFrame({"n": [1]}, geometry=[Point(12.1, 49.0)], crs="EPSG:4326").to_file(
        cache / "pts.gpkg", driver="GPKG")
    return str(tmp_path)


def _names(tools) -> set[str]:
    return {t.__name__ for t in tools}


def test_each_ressort_gets_its_slice_the_checks_and_its_agent_level_tools(tmp_path):
    ws = str(tmp_path)
    vector = _names(ressorts.ressort_tools("vector", ws, GEODATA))
    assert vector == set(ressortcut.tools_for("vector")) | {"geo_python_run", "inspect_map"}
    data = _names(ressorts.ressort_tools("data", ws, GEODATA))
    assert data == set(ressortcut.tools_for("data")) | {"inspect_map"}
    assert "geo_python_run" not in data and "vector_buffer" not in data


def test_instructions_carry_role_text_and_contract_once():
    for name in ressortcut.RESSORTS:
        text = ressorts.ressort_instructions(name)
        assert text.startswith(ressorts.role(name))
        assert "## Handing back" in text
    vector = ressorts.ressort_instructions("vector")
    assert "geo_python_run" in vector  # the vector text explains the escape hatch
    data = ressorts.ressort_instructions("data")
    # eight acquisition modules share one text — it must appear once, not eight times
    from chester import discoveryshared

    assert data.count(discoveryshared.instructions()[:80]) == 1


def test_an_unknown_ressort_is_refused():
    with pytest.raises(ValueError, match="unknown ressort"):
        ressorts.build_ressort_agent("statistics", model=FunctionModel(lambda m, i: None))


def _scripted(first_call: ToolCallPart | None):
    """Model: call one tool (if given), then hand back — forgetting to list the file."""
    def respond(messages, info: AgentInfo) -> ModelResponse:
        if first_call is not None and len(messages) == 1:
            return ModelResponse(parts=[first_call])
        out = info.output_tools[0]
        return ModelResponse(parts=[ToolCallPart(
            out.name, {"outputs": [], "report": "reprojected", "open_points": []})])
    return FunctionModel(respond)


def test_produced_paths_come_back_even_if_the_model_forgets_them(tmp_path):
    ws = _workspace(tmp_path)
    call = ToolCallPart("vector_reproject", {"input_path": "pts.gpkg",
                                             "output_path": "pts_25832.gpkg",
                                             "target_crs": "EPSG:25832"})
    agent = ressorts.build_ressort_agent("vector", ws, model=_scripted(call), geodata=GEODATA)
    result = asyncio.run(ressorts.run_ressort("vector", "reproject", ["pts.gpkg"],
                                              workspace=ws, agent=agent))
    assert result["ok"] and not result["capped"]
    assert result["tools_called"] == ["vector_reproject"]
    assert any(p.endswith("pts_25832.gpkg") for p in result["outputs"]), result
    record = json.loads(Path(result["log"]).read_text().splitlines()[-1])
    assert record["ressort"] == "vector" and record["tools_called"] == ["vector_reproject"]


def test_each_output_is_listed_once_and_absolute(tmp_path):
    """Found in the first real run: the model listed `x.gpkg`, the tool return held the
    resolved path, and the same file came back twice under two spellings."""
    ws = _workspace(tmp_path)
    call = ToolCallPart("vector_reproject", {"input_path": "pts.gpkg",
                                             "output_path": "pts_25832.gpkg",
                                             "target_crs": "EPSG:25832"})

    def respond(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[call])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "outputs": ["pts_25832.gpkg"], "report": "done", "open_points": []})])

    agent = ressorts.build_ressort_agent("vector", ws, model=FunctionModel(respond),
                                         geodata=GEODATA)
    result = asyncio.run(ressorts.run_ressort("vector", "reproject", workspace=ws,
                                              agent=agent))
    assert len(result["outputs"]) == 1, result["outputs"]
    assert Path(result["outputs"][0]).is_absolute()


def test_a_request_cap_says_that_it_capped(tmp_path):
    ws = _workspace(tmp_path)

    def always_a_tool(messages, info):
        return ModelResponse(parts=[ToolCallPart("vector_info", {"path": "pts.gpkg"})])

    agent = ressorts.build_ressort_agent("vector", ws, model=FunctionModel(always_a_tool),
                                         geodata=GEODATA)
    result = asyncio.run(ressorts.run_ressort("vector", "loop", workspace=ws, agent=agent,
                                              request_limit=2))
    assert result["capped"] and not result["ok"]
    assert "request limit of 2" in result["cap"]
    assert "Stopped at the" in result["report"]
    assert result["tools_called"], "the calls before the cap must still be recorded"


def test_a_time_cap_says_that_it_capped(tmp_path):
    ws = _workspace(tmp_path)

    async def slow(messages, info):
        await asyncio.sleep(5)
        return ModelResponse(parts=[TextPart("never")])

    agent = ressorts.build_ressort_agent("vector", ws, model=FunctionModel(slow),
                                         geodata=GEODATA)
    result = asyncio.run(ressorts.run_ressort("vector", "slow", workspace=ws, agent=agent,
                                              timeout_s=0.2))
    assert result["capped"] and "time limit" in result["cap"]


def test_a_failing_ressort_reports_instead_of_raising(tmp_path):
    """The first team run died here: a ressort never produced a valid handover, and
    the exception ended the orchestrator's run. A ressort must hand back a failure."""
    ws = _workspace(tmp_path)

    def broken_handover(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"nope": 1})])

    agent = ressorts.build_ressort_agent("data", ws, model=FunctionModel(broken_handover),
                                         geodata=GEODATA)
    result = asyncio.run(ressorts.run_ressort("data", "list", workspace=ws, agent=agent))
    assert result["ok"] is False and result["error"] and not result["capped"]
    assert "UnexpectedModelBehavior" in result["error"]
    assert result["report"].startswith("Failed:")
    assert Path(result["log"]).is_file()


def test_the_ressorts_run_under_the_run_s_own_config(tmp_path):
    """Found in review (2026-09-20): `testprompt.py --model X` put the orchestrator on X
    while every ressort stayed on the main config's model — and the run was archived
    under X. The config of the run has to reach the ressorts."""
    (tmp_path / "side.json").write_text(json.dumps(
        {"model": {"model": "ollama/main-x"}, "team": {"ressort_model": "ollama/ressort-y"}}))
    assert ressorts._model_name("side.json", str(tmp_path)) == "ollama/ressort-y"
    (tmp_path / "plain.json").write_text(json.dumps({"model": {"model": "ollama/main-x"}}))
    # No `team.ressort_model` → the main model *of that config*, not of the live one.
    from chester.runtime import config as runtime_config

    assert runtime_config.config_block("team", "plain.json", str(tmp_path)) == {}


def test_the_configured_retention_reaches_the_cache_tools():
    """Without `ttl_days` a ressort's `geocache_list` prunes on the 30-day default and
    deletes data the user asked to keep for longer (review, 2026-09-20)."""
    options = ressorts._wrapper_options({"roots": [], "postgis": None,
                                         "stac_catalogs": None, "ttl_by_source": {},
                                         "ttl_days": 180})
    assert options["inventorytools"]["default_ttl_days"] == 180


def test_a_url_is_not_a_produced_file(tmp_path):
    """A STAC or catalogue return is full of `https://host/x.csv`. Resolving those as
    paths created folders named after hosts in the cache (review, 2026-09-20)."""
    ws = _workspace(tmp_path)
    content = {"items": ["https://example.org/data/x.csv", "s3://bucket/y.tif"],
               "output": "pts.gpkg"}
    produced = ressorts._produced(content, ws)
    assert produced == [str(Path(ws) / "geocache" / "pts.gpkg")]
    assert not list((Path(ws) / "geocache").glob("https:*")), "no directory may be created"
    assert not (Path(ws) / "geocache" / "data").exists()


def test_the_ressort_modules_come_from_the_tools_themselves():
    """Which module a tool belongs to — and so which instruction text applies — is read
    off `__module__`, not kept as a second list beside the cut."""
    assert "vectortools" in ressorts._owning_modules("vector")
    assert "demtools" in ressorts._owning_modules("data")
    assert "validationtools" in ressorts._owning_modules("output")  # the checks travel


def test_a_ressort_points_at_the_one_that_can_do_it():
    """The first bench run (2026-09-20): the orchestrator asked the fetching ressort to
    build a point layer from coordinates — which only the vector ressort can do — and it
    tried `geodataset_fetch` 22 times until the request cap stopped it. Roles and
    contract now say where such a task belongs and that trying variants is wrong."""
    assert "VECTOR" in ressorts.role("data"), "the data ressort must name the way out"
    assert "coordinates" in ressorts.role("vector")
    contract = ressorts.ressort_instructions("data")
    assert "do not try variants" in contract
    tools = _names(ressorts.ressort_tools("data", "/tmp/x", GEODATA))
    assert "geo_python_run" not in tools, "the role only holds while it cannot build layers"


def test_the_log_says_why_a_tool_call_failed(tmp_path):
    """After a ressort called the same wrong tool 22 times (2026-09-20), the log held
    only the names. The outcome of each call belongs in the file — and only there: the
    return to the orchestrator must stay small enough not to be offloaded."""
    ws = _workspace(tmp_path)
    bad = ToolCallPart("vector_reproject", {"input_path": "nope.gpkg",
                                            "output_path": "out.gpkg",
                                            "target_crs": "EPSG:25832"})
    agent = ressorts.build_ressort_agent("vector", ws, model=_scripted(bad), geodata=GEODATA)
    result = asyncio.run(ressorts.run_ressort("vector", "fail", workspace=ws, agent=agent))
    record = json.loads(Path(result["log"]).read_text().splitlines()[-1])
    calls = record["calls"]
    assert [c["tool"] for c in calls][:1] == ["vector_reproject"]
    assert calls[0]["ok"] is False and calls[0]["error"], "the log must say why"
    assert len(calls[0]["error"]) <= 200
    assert "calls" not in result, "the handover stays small; the why lives in the log"


def test_every_inner_call_is_shown_while_it_happens(tmp_path):
    """A ressort call takes minutes, and until it returns the watcher saw nothing —
    only the summary afterwards (2026-09-20). Each call and its result now go to the
    live channel the runner publishes (`chester.runtime.live`)."""
    from chester.runtime import live

    ws = _workspace(tmp_path)
    call = ToolCallPart("vector_reproject", {"input_path": "pts.gpkg",
                                             "output_path": "pts_25832.gpkg",
                                             "target_crs": "EPSG:25832"})
    agent = ressorts.build_ressort_agent("vector", ws, model=_scripted(call), geodata=GEODATA)
    seen: list[str] = []
    with live.use_sink(seen.append):
        asyncio.run(ressorts.run_ressort("vector", "reproject", workspace=ws, agent=agent))
    shown = "".join(seen)
    assert "[vector] → vector_reproject(" in shown, "the call, as it starts"
    assert "[vector] ← vector_reproject:" in shown, "and its result"
    assert "final_result" not in shown, "the handover is not a tool call"


def test_without_a_watcher_nothing_is_emitted(tmp_path):
    """No sink, no output — every single-agent run and every test runs that way."""
    from chester.runtime import live

    assert live.emit("anything") is None
    assert live.short({"a": "x" * 50}, 20).endswith("… (+39)")


def test_prose_counts_as_a_handover(tmp_path):
    """Measured 2026-09-20: a ressort did its work, then answered in prose three times
    ("I have listed the files…") and the run died on the output schema — work done,
    result lost. Prose is now a valid handover; the paths come from the tool returns."""
    ws = _workspace(tmp_path)
    call = ToolCallPart("vector_reproject", {"input_path": "pts.gpkg",
                                             "output_path": "pts_25832.gpkg",
                                             "target_crs": "EPSG:25832"})

    def prose(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[call])
        return ModelResponse(parts=[TextPart("Ich habe die Datei umprojiziert.")])

    agent = ressorts.build_ressort_agent("vector", ws, model=FunctionModel(prose),
                                         geodata=GEODATA)
    result = asyncio.run(ressorts.run_ressort("vector", "reproject", workspace=ws,
                                              agent=agent))
    assert result["ok"] and not result["capped"] and not result["error"]
    assert result["report"] == "Ich habe die Datei umprojiziert."
    assert any(p.endswith("pts_25832.gpkg") for p in result["outputs"]), result


def test_nobody_in_the_team_gets_the_skills_and_the_knowledge_is_in_the_tool_text():
    """Both places were tried in one day (2026-09-21) and both cost a run.

    The orchestrator read `walkability` and passed `qgis_service_area` down — a tool
    it does not have and nobody in the team has. The ressorts then got the catalogue
    instead, and the output ressort loaded four skills in 215 s, three of them recipes
    for phases it does not serve, and went on to call `geodatasets_list`, `vector_info`
    and `geo_python_run` — none of which it has — until its time limit stopped it with
    nothing produced. A skill is a recipe for the whole chain: 8 of the 9 name tools
    from two to four ressorts.

    What the skill knew has to be somewhere, so it is in the tool text, which is always
    in the prompt and names a tool that exists."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    seen: dict = {}

    def respond(messages, info: AgentInfo) -> ModelResponse:
        seen["tools"] = [t.name for t in info.function_tools]
        seen["instructions"] = info.instructions or ""
        return ModelResponse(parts=[TextPart("done")])

    agent = ressorts.build_ressort_agent("vector", ".chester/workspace",
                                         model=FunctionModel(respond), geodata=GEODATA)
    asyncio.run(agent.run("x"))
    assert "load_capability" not in seen["tools"], "no catalogue for a ressort"
    assert "walkability" not in seen["instructions"]
    # The one piece of knowledge that a skill carried and a run actually needed.
    assert "never a buffer" in seen["instructions"], "the rule moved into the tool text"
    assert "service_area" in seen["instructions"]

    from chester.team.orchestrator import team_capabilities

    names = [type(c).__name__ for c in team_capabilities(".chester/workspace")]
    assert not any("Skill" in n for n in names), f"orchestrator got a skill capability: {names}"


def test_a_ressort_can_hand_work_back_instead_of_failing(tmp_path):
    """The other direction of the chain (21.09.2026): a ressort that lacks a condition
    it cannot bring about itself — layers in different CRS, a boundary it was not
    given — states the *condition* and hands back. Handing back with nothing produced
    is a correct answer, so it must not read as a failure: `ok` stays true, and the
    need travels in the return, the summary line and the log."""
    ws = _workspace(tmp_path)

    def asks_back(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "outputs": [], "report": "Cannot draw the map: the two layers differ in CRS.",
            "needs": ["both layers in one metric CRS"]})])

    agent = ressorts.build_ressort_agent("output", ws, model=FunctionModel(asks_back),
                                         geodata=GEODATA)
    result = asyncio.run(ressorts.run_ressort("output", "map it", workspace=ws, agent=agent))
    assert result["ok"] is True and not result["capped"] and not result["error"]
    assert result["outputs"] == [], "asking back produces nothing — and that is allowed"
    assert result["needs"] == ["both layers in one metric CRS"]
    assert json.loads(Path(result["log"]).read_text().splitlines()[-1])["needs"]

    from ask import _ressort_line

    assert "braucht: both layers in one metric CRS" in _ressort_line("ressort_output", result)


def test_both_sides_of_the_back_channel_ask_for_a_goal_not_a_recipe():
    """The symmetry of the rule the orchestrator got on 21.09.: it hands out goals, so
    a ressort states a need as a condition. A ressort does not know the others' tools;
    a step it invents costs the team a run — the mistake already measured in the other
    direction, when the orchestrator passed `qgis_service_area` down."""
    contract = ressorts.ressort_instructions("output")
    assert "Say a **condition**, not a recipe" in contract
    assert "not `open_points`" in contract, "a need blocks, an open point is a doubt"

    from chester.team.orchestrator import _INSTRUCTIONS

    assert "`needs`" in _INSTRUCTIONS and "not a failure" in _INSTRUCTIONS
    assert "same need twice" in _INSTRUCTIONS, "an unanswerable need must not loop"


def test_the_call_that_killed_the_run_is_in_the_log(tmp_path):
    """The gap hit exactly the call one wants to see (2026-09-21).

    A call is recorded when the model makes it, its outcome when the result arrives.
    An exception out of a tool ends the run in between: `vector_intersection` raised,
    nine of ten calls were in the log, and the tenth — the only one that mattered —
    was not. It is there now, marked unanswered.
    """
    ws = _workspace(tmp_path)
    result = {
        "ok": False, "ressort": "vector", "outputs": [],
        "error": "NotImplementedError: df1 contains mixed geometry types.",
        "tools_called": ["vector_info", "vector_reproject", "vector_intersection"],
    }
    outcomes = [{"tool": "vector_info", "ok": True, "error": ""},
                {"tool": "vector_reproject", "ok": True, "error": ""}]
    path = ressorts._write_log(ws, "clip the supermarkets", result, outcomes)

    logged = json.loads(Path(path).read_text().splitlines()[-1])["calls"]
    assert [c["tool"] for c in logged] == result["tools_called"], "none falls out"
    assert logged[-1]["ok"] is False
    assert "the run ended inside this call" in logged[-1]["error"]


def test_a_call_that_answered_is_not_reported_as_unanswered():
    """The entry fills a gap, it does not duplicate: what came back is listed once.
    Counted per tool, so the same name twice counts twice."""
    assert ressorts._unanswered(["a", "b"], [{"tool": "a", "ok": True},
                                             {"tool": "b", "ok": True}]) == []
    missing = ressorts._unanswered(["a", "a"], [{"tool": "a", "ok": True}])
    assert [m["tool"] for m in missing] == ["a"]


def test_a_labelled_input_path_is_accepted_and_its_role_reaches_the_prompt():
    """`{"path": …, "type": "boundary"}` is one input with a role, not a schema error.

    Measured 2026-09-26 (`cycleway-length` against the team): the orchestrator handed
    over labelled paths twice — once to `ressort_vector`, once to `ressort_output` —
    and pydantic rejected both calls because the signature said `list[str]`. Each
    rejection cost a model round; on Test-Level 4, where two dialogues died at the
    900 s cap, rounds like these are the budget. The second occurrence is what made it
    a finding: the spelling is stable, so the signature was the narrow part.

    And the label carries information the ressort otherwise has to guess: which of two
    layers is the boundary. It is therefore written into the prompt, not dropped.
    """
    prompt = ressorts._prompt("Clip the paths.", [
        {"path": "/ws/geocache/boundary.gpkg", "type": "boundary"},
        "/ws/geocache/paths.gpkg",
    ])
    assert "- /ws/geocache/boundary.gpkg (boundary)" in prompt
    assert "- /ws/geocache/paths.gpkg" in prompt
    assert "(boundary)" in prompt.split("paths.gpkg")[0], "role belongs to its own path"


def test_the_role_is_read_under_any_of_its_plausible_keys():
    """`type` is what the orchestrator wrote; the synonyms spare a second rejection."""
    for key in ("type", "role", "kind", "as", "label"):
        path, role = ressorts._labelled({"path": "/ws/a.gpkg", key: "boundary"})
        assert (path, role) == ("/ws/a.gpkg", "boundary"), key
    assert ressorts._labelled({"input_path": "/ws/b.gpkg"}) == ("/ws/b.gpkg", "")
    assert ressorts._labelled("/ws/c.gpkg") == ("/ws/c.gpkg", "")


def test_an_unusable_input_entry_does_not_become_an_empty_bullet():
    """A dict without any path key is dropped, not listed as `- ` with nothing after."""
    prompt = ressorts._prompt("Do it.", [{"type": "boundary"}, "/ws/real.gpkg"])
    assert "- /ws/real.gpkg" in prompt
    assert "\n- \n" not in prompt and not prompt.endswith("- ")


def test_a_ressort_hands_its_own_slice_to_the_snippet_guard(tmp_path):
    """The guard inside `geo_python_run` has to know its ressort's toolset.

    Otherwise it points at other ressorts' tools — on 2026-09-27 three times at
    `zonal_stats`, which belongs to the raster ressort, while the vector ressort fell
    back on hand-rolled code and produced eighteen wrong district means.
    """
    tools = ressorts.ressort_tools("vector", _workspace(tmp_path), GEODATA)
    names = {t.__name__ for t in tools}
    assert "geo_python_run" in names
    assert "zonal_stats" not in names, "zonal statistics is raster, not vector"
    guard = next(t for t in tools if t.__name__ == "geo_python_run")
    bound = [c.cell_contents for c in (guard.__closure__ or ())
             if isinstance(c.cell_contents, frozenset)]
    assert bound, "`geo_python_run` does not know its ressort's toolset"
    available = bound[0]
    assert "vector_clip" in available and "geo_python_run" in available
    assert "zonal_stats" not in available
    # And the refusal then points into the snippet instead of at a foreign tool.
    from chester.runtime.geopython import _refusal
    text = _refusal([("zonal_stats", "masks nodata out")], available)
    assert "already bound in this snippet" in text
    assert "call them directly" not in text


def test_a_catalogue_listing_does_not_become_the_ressorts_outputs(tmp_path):
    """`geocache_list` answers what exists — none of it was produced by this call.

    Measured 2026-09-27 (`city3d-regensburg-dom-height`): the data ressort listed the
    cache while looking for data, and all 91 entries were harvested into `outputs`. The
    orchestrator then handed the vector ressort `buildings.gpkg` — a three-object
    fixture from the previous evening's probe run, seven kilometres from the cathedral.
    Two honest `ok: false` handovers and four minutes of diagnosis followed.
    """
    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    ws = _workspace(tmp_path)
    existing = str(Path(ws) / "geocache" / "buildings.gpkg")
    gpd.GeoDataFrame({"n": [1]}, geometry=[Point(12.1, 49.0)], crs="EPSG:4326").to_file(
        existing, driver="GPKG")
    catalogue = {"ok": True, "count": 2, "datasets": [
        {"dataset": "geocache/buildings.gpkg", "path": existing},
        {"dataset": "geocache/other.gpkg", "path": existing},
    ]}

    for tool, expected in (("geocache_list", []), ("osm_features", [existing])):
        node = types.SimpleNamespace(
            model_response=None,
            request=ModelRequest(parts=[
                ToolReturnPart(tool_name=tool, content=catalogue, tool_call_id="c1")]))
        produced: list[str] = []
        ressorts._record(node, [], produced, ws)
        assert produced == expected, f"{tool}: {produced}"


def test_every_listing_tool_named_in_the_rule_really_exists():
    """A renamed tool must not slip out of the rule and start polluting again."""
    surface = {t for tools in ressortcut.RESSORTS.values() for t in tools}
    unknown = sorted(ressorts.REPORTS_WHAT_EXISTS - surface)
    assert not unknown, f"named in REPORTS_WHAT_EXISTS but not a tool: {unknown}"


def test_the_contract_asks_for_values_not_verbs():
    """The rule has to be in the text the ressort actually reads, with its example."""
    contract = ressorts._CONTRACT
    assert "names the result, not the route" in contract
    assert "107.2" in contract, "the measured example makes the rule concrete"
    assert "cannot open your files" in contract
