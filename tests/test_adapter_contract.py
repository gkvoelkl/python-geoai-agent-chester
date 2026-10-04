"""One case, one promise, every adapter (KP.2).

Chester's tools reach a model three ways: as capabilities of the single agent, as the
slice a team ressort gets, and over MCP to a foreign client. Until now each adapter was
tested on its own (`test_geo_capabilities.py`, `test_ressorts.py`, `test_mcpserver.py`),
and a difference between them showed only to someone reading all three files. The
measurements of 2026-10-04 compared exactly these adapters cell against cell — which is
only fair if the same tool keeps the same promise in each.

Each case below is written once and run through all three. The MCP side goes through
the protocol (an in-memory client), not around it, so serialisation is part of what is
checked. A case that fails for one adapter only is the defect this file exists for.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from _util import tools_of
from shapely.geometry import Point

from chester import mcpserver, ressortcut
from chester.capabilities.perception import PerceptionCapability
from chester.capabilities.vector import VectorCapability
from chester.team import ressorts

GEODATA: dict = {"roots": [], "postgis": None, "stac_catalogs": None, "ttl_by_source": {}}
ADAPTERS = ("agent", "team", "mcp")


def _agent(ws: str) -> Callable[..., dict]:
    tools = tools_of(VectorCapability(workspace=ws)) | tools_of(PerceptionCapability(workspace=ws))
    return lambda name, **kw: tools[name](**kw)


def _team(ws: str) -> Callable[..., dict]:
    def call(name: str, **kw) -> dict:
        slice_ = ressorts.ressort_tools(ressortcut.ressort_of(name), ws, GEODATA)
        return next(t for t in slice_ if t.__name__ == name)(**kw)
    return call


def _mcp(ws: str) -> Callable[..., dict]:
    from fastmcp import Client

    server = mcpserver.build_server(ws)

    async def run(name: str, kw: dict) -> dict:
        async with Client(server) as client:
            res = await client.call_tool(name, kw, raise_on_error=False)
        data = res.structured_content
        if data is None:  # fall back to the text block
            data = json.loads(res.content[0].text)
        return data.get("result", data) if set(data) == {"result"} else data

    return lambda name, **kw: asyncio.run(run(name, kw))


@pytest.fixture(params=ADAPTERS)
def call(request, tmp_path) -> Callable[..., dict]:
    if request.param == "mcp":
        pytest.importorskip("fastmcp")
    ws = str(tmp_path)
    cache = tmp_path / "geocache"
    cache.mkdir()
    gpd.GeoDataFrame({"n": [1, 2]}, geometry=[Point(12.10, 49.01), Point(12.11, 49.02)],
                     crs="EPSG:4326").to_file(cache / "pts.gpkg", driver="GPKG")
    _rgb(cache / "aerial_rgb.tif")
    fn = {"agent": _agent, "team": _team, "mcp": _mcp}[request.param](ws)
    fn.cache = cache  # type: ignore[attr-defined]
    return fn


def _rgb(path: Path) -> None:
    import rasterio
    from rasterio.transform import from_origin

    profile = {"driver": "GTiff", "height": 8, "width": 8, "count": 3, "dtype": "uint16",
               "crs": "EPSG:25832", "transform": from_origin(700000, 5400000, 1.0, 1.0)}
    rng = np.random.default_rng(0)
    with rasterio.open(path, "w", **profile) as ds:
        for i in range(1, 4):
            ds.write(rng.integers(40, 210, size=(8, 8), dtype="uint16"), i)


def test_a_writing_tool_returns_its_path_and_stamps_provenance(call):
    res = call("vector_reproject", input_path="pts.gpkg", output_path="pts_25832.gpkg",
               target_crs="EPSG:25832")
    assert res["ok"] is True
    out = Path(res["output"])
    assert out.is_file() and out.parent == call.cache, "the output lands in the GeoCache"
    assert Path(f"{out}.meta.json").is_file(), "no sidecar — the gate cannot trace it"
    assert gpd.read_file(out).crs.to_epsg() == 25832


def test_a_buffer_in_degrees_is_refused_not_drawn(call):
    res = call("vector_buffer", input_path="pts.gpkg", output_path="buf.gpkg", distance=500)
    assert res["ok"] is False
    assert not (call.cache / "buf.gpkg").exists()


def test_a_missing_layer_is_an_answer_not_a_crash(call):
    res = call("vector_info", path="gibtsnicht.gpkg")
    assert res["ok"] is False and res.get("error")


def test_no_index_name_turns_rgb_into_nir(call):
    res = call("spectral_index", band_a="aerial_rgb.tif", band_b="aerial_rgb.tif",
               output_path="probe_ndvi.tif", kind="ndwi")
    assert res["ok"] is False and res.get("has_nir") is False
    assert not (call.cache / "probe_ndvi.tif").exists()


def test_every_adapter_shows_the_same_description(tmp_path):
    """The docstring is the tool text everywhere — a second wording would drift."""
    ws = str(tmp_path)
    agent = tools_of(VectorCapability(workspace=ws)) | tools_of(PerceptionCapability(workspace=ws))
    served = {t.__name__: t for t in mcpserver.collect_tools(ws)}
    for name, fn in agent.items():
        if name not in served:
            continue
        assert (fn.__doc__ or "") == (served[name].__doc__ or ""), name
        owner = ressortcut.ressort_of(name)
        if owner:
            team = {t.__name__: t for t in ressorts.ressort_tools(owner, ws, GEODATA)}
            assert (team[name].__doc__ or "") == (fn.__doc__ or ""), name
