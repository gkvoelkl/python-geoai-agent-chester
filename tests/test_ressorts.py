"""Test-Level 1: the ressort agents of chester-team, without a language model.

A `FunctionModel` scripts the model's side — one tool call, then the handover — so the
wrapper itself is under test: which tools a ressort gets, what it is told, that the
produced paths come back even when the model forgets to list them, that a cap says it
capped, and that every call leaves a record. The real model run is Test-Level 2+.
"""

from __future__ import annotations

import asyncio
import json
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
    scout = _names(ressorts.ressort_tools("scout", ws, GEODATA))
    assert scout == set(ressortcut.tools_for("scout")) | {"inspect_map"}
    assert "geo_python_run" not in scout and "vector_buffer" not in scout


def test_instructions_carry_role_text_and_contract_once():
    for name in ressortcut.RESSORTS:
        text = ressorts.ressort_instructions(name)
        assert text.startswith(ressorts.role(name))
        assert "## Handing back" in text
    vector = ressorts.ressort_instructions("vector")
    assert "geo_python_run" in vector  # the vector text explains the escape hatch
    acq = ressorts.ressort_instructions("acquisition")
    # eight acquisition modules share one text — it must appear once, not eight times
    from chester import discoveryshared

    assert acq.count(discoveryshared.instructions()[:80]) == 1


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
    """The first team run died here: the scout never produced a valid handover, and
    the exception ended the orchestrator's run. A ressort must hand back a failure."""
    ws = _workspace(tmp_path)

    def broken_handover(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"nope": 1})])

    agent = ressorts.build_ressort_agent("scout", ws, model=FunctionModel(broken_handover),
                                         geodata=GEODATA)
    result = asyncio.run(ressorts.run_ressort("scout", "list", workspace=ws, agent=agent))
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
    assert "demtools" in ressorts._owning_modules("acquisition")
    assert "validationtools" in ressorts._owning_modules("output")  # the checks travel


def test_a_ressort_points_at_the_one_that_can_do_it():
    """The first bench run (2026-09-20): the orchestrator asked acquisition to build a
    point layer from coordinates — which only the vector ressort can do — and it tried
    `geodataset_fetch` 22 times until the request cap stopped it. Roles and contract now
    say where such a task belongs and that trying variants is wrong."""
    assert "VECTOR" in ressorts.role("acquisition"), "acquisition must name the way out"
    assert "coordinates" in ressorts.role("vector")
    contract = ressorts.ressort_instructions("acquisition")
    assert "do not try variants" in contract
    tools = _names(ressorts.ressort_tools("acquisition", "/tmp/x", GEODATA))
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
    """Measured 2026-09-20: the scout did its work, then answered in prose three times
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


def test_the_ressorts_get_the_skills_and_the_orchestrator_does_not():
    """The first team run turned "within a 10-minute walk" into an 800 m straight-line
    buffer — the mistake `walkability` warns about in its first line. Skills belong
    where the tools are (2026-09-21): deferred, so only the catalogue is in the prompt."""
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
    assert "load_capability" in seen["tools"], "the catalogue is reachable"
    assert "walkability" in seen["instructions"], "and listed"
    assert "SKILL.md" not in seen["instructions"], "deferred: the body is not in the prompt"
