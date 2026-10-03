"""A ressort call cut off from outside still leaves its line in the log.

Measured 2026-10-03, probe `ndvi-without-nir` against the team: the probe's 900-second
limit cancelled the orchestrator while a raster ressort was running. That ressort wrote
the file the probe forbids — and the log had no line for it, because the cancellation
went past `_write_log`. The one call that needed explaining was the one not recorded.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import geopandas as gpd
import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from shapely.geometry import Point

from chester.team import ressorts

GEODATA: dict = {"roots": [], "postgis": None, "stac_catalogs": None, "ttl_by_source": {}}


def _hanging_model() -> FunctionModel:
    """Call one tool, then never answer — the ressort is still busy when the clock runs out."""
    async def respond(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("vector_info", {"path": "pts.gpkg"})])
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    return FunctionModel(respond)


def test_a_cancelled_ressort_call_is_logged_and_the_cancellation_goes_on(tmp_path):
    cache = tmp_path / "geocache"
    cache.mkdir()
    gpd.GeoDataFrame({"n": [1]}, geometry=[Point(12.1, 49.0)], crs="EPSG:4326").to_file(
        cache / "pts.gpkg", driver="GPKG")
    ws = str(tmp_path)
    agent = ressorts.build_ressort_agent("vector", ws, model=_hanging_model(), geodata=GEODATA)

    async def caller() -> None:
        # The caller's own clock, as `probe.py` wraps the orchestrator; the ressort's
        # internal limit is far away, so only the outer cancellation can stop it.
        await asyncio.wait_for(
            ressorts.run_ressort("vector", "inspect pts", workspace=ws, agent=agent,
                                 timeout_s=600), timeout=5)

    with pytest.raises(TimeoutError):  # the cancellation is not swallowed
        asyncio.run(caller())

    log = Path(ws) / "team-runs" / "ressort-calls.jsonl"
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["ressort"] == "vector" and entry["ok"] is False
    assert entry["error"] == "cancelled by the caller"
    assert entry["tools_called"] == ["vector_info"]
    assert entry["calls"][0]["tool"] == "vector_info" and entry["calls"][0]["ok"] is True
    assert entry["calls"][0]["values"], "what the tool answered is kept as well"
