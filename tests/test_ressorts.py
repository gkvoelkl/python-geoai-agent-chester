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
        assert text.startswith(ressorts._ROLE[name])
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
