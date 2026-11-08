"""The declared tool results (`chester.toolreturns`) against what the tools really return.

Test-Level 1 — no model. The offline tools run on the Test-Level-2 fixtures; the ones that
need the network (geocode, OSM, boundaries, DEM, catalog, WFS) run with ``--run-network``.
A result type that drifts from the tool — a key renamed, a key added, a count turned into
a list — fails here instead of misleading a model through the `run_code` stub.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import get_args, get_origin, get_type_hints, is_typeddict

import pytest
from pydantic import TypeAdapter, ValidationError

from chester import toolreturns, wrapperlayer

FIXTURES = Path(__file__).parent.parent / "probes" / "fixtures"
_REGENSBURG_WFS = ("https://mapservice.regensburg.de/cgi-bin/mapserv"
                   "?map=%2Fdata%2Fows%2Fmaps%2Fkleingliederung_wfs.map")


def problems(name: str, result: dict) -> list[str]:
    """Everything in ``result`` that its declared type does not say — empty means it fits.

    Walks nested types too (a catalog resource inside a candidate inside the result): a
    key missing, undeclared or of the wrong type at any depth is named with its path.
    """
    if result.get("ok") is not True:
        return [f"{name} failed: {result.get('error')}"]
    out: list[str] = []
    _walk(result, toolreturns.RETURNS[name], name, out)
    return out


def _walk(value, typ, path: str, out: list[str]) -> None:
    """Check ``value`` against ``typ`` field by field; a null `NULLABLE` allows passes."""
    if isinstance(value, list) and get_origin(typ) is list:
        (item,) = get_args(typ)
        for i, v in enumerate(value):
            _walk(v, item, f"{path}[{i}]", out)
        return
    if isinstance(value, dict) and is_typeddict(typ):
        hints = get_type_hints(typ)
        nullable = toolreturns.NULLABLE.get(typ.__name__, frozenset())
        out += [f"{path}: required key {k!r} missing" for k in typ.__required_keys__ - set(value)]
        out += [f"{path}: undeclared key {k!r}" for k in set(value) - set(hints)]
        for k, v in value.items():
            if k in hints and not (k in nullable and v is None):
                _walk(v, hints[k], f"{path}.{k}", out)
        return
    try:
        TypeAdapter(typ).validate_python(value)
    except ValidationError as exc:
        out.append(f"{path}: {exc.errors()[0]['msg']}")


@pytest.fixture
def tools(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    for f in FIXTURES.iterdir():
        shutil.copy(f, ws / f.name)
    return ws, {t.__name__: t for t in wrapperlayer.collect_tools(str(ws))}


def test_every_declared_tool_exists(tools):
    _ws, real = tools
    assert set(toolreturns.RETURNS) <= set(real), sorted(set(toolreturns.RETURNS) - set(real))


def test_typed_sets_the_declared_type_and_leaves_the_rest(tools):
    _ws, real = tools
    for name, tool in real.items():
        toolreturns.typed(tool)
        want = toolreturns.RETURNS.get(name)
        if want is not None:
            assert tool.__annotations__["return"] is want, name
        else:
            assert tool.__annotations__["return"] in (dict, "dict"), name


def test_offline_tools_return_what_they_declare(tools):
    ws, t = tools
    runs = {
        "vector_info": t["vector_info"](path=str(ws / "zones.gpkg"), values_of="name"),
        "vector_reproject": t["vector_reproject"](
            input_path=str(ws / "schools_4326.gpkg"), output_path="schools_25832.gpkg",
            target_crs="EPSG:25832"),
        "vector_buffer": t["vector_buffer"](
            input_path=str(ws / "stops.gpkg"), output_path="stops_buffer.gpkg", distance=100),
        "vector_clip": t["vector_clip"](
            input_path=str(ws / "buildings.gpkg"), overlay_path=str(ws / "district.gpkg"),
            output_path="buildings_clip.gpkg"),
        "vector_filter": t["vector_filter"](
            path=str(ws / "buildings.gpkg"), expression="hoehe_m > 0",
            output_path="buildings_high.gpkg"),
        "sample_raster": t["sample_raster"](
            raster_path=str(ws / "aerial_rgb.tif"), points_path=str(ws / "stops.gpkg"),
            output_path="stops_sampled.gpkg"),
        "zonal_stats": t["zonal_stats"](
            raster_path=str(ws / "aerial_rgb.tif"), zones_path=str(ws / "zones.gpkg"),
            output_path="zones_stats.gpkg"),
    }
    merged = t["vector_merge"](input_paths=[str(ws / "stops.gpkg"), str(ws / "buildings.gpkg")],
                               output_path="mixed.gpkg")
    runs["vector_merge"] = merged
    runs["vector_split_by_geometry"] = t["vector_split_by_geometry"](
        path=merged["output"], output_prefix="mixed")
    runs["render_map"] = t["render_map"](output_path="map.html",
                                         layers=[str(ws / "zones.gpkg")])
    found = [p for name, result in runs.items() for p in problems(name, result)]
    assert not found, found


def test_a_split_part_is_described_too(tools):
    """`parts` is where gemma4 and Nemotron went wrong — its items are typed, not `Any`."""
    ws, t = tools
    merged = t["vector_merge"](input_paths=[str(ws / "stops.gpkg"), str(ws / "buildings.gpkg")],
                               output_path="mixed.gpkg")
    split = t["vector_split_by_geometry"](path=merged["output"], output_prefix="mixed")
    families = {p["geometry_type"] for p in split["parts"]}
    assert {"Point", "Polygon"} <= families
    assert all(set(p) == set(toolreturns.SplitPart.__annotations__) for p in split["parts"])


def test_a_mismatch_is_caught():
    """The checker itself: a count as a list and an unknown key both fail."""
    bad = {"ok": True, "output": "/ws/x.gpkg", "features": [1, 2], "geometry_types": [],
           "crs": "EPSG:4326", "surprise": 1}
    found = problems("osm_features", bad)
    assert any("features" in p for p in found)
    assert any("surprise" in p for p in found)
    nested = {"ok": True, "query": "q", "catalog": "c", "total": 1, "count": 1,
              "candidates": [{"title": "t", "publisher": "p", "license": None,
                              "resources": [{"service": "WFS", "url": "u", "wfs_url": None,
                                             "typename": None, "format": "WFS"}]}]}
    found = problems("geodata_search", nested)
    assert found == ["geodata_search.candidates[0].resources[0]: undeclared key 'format'"], found


@pytest.mark.network
def test_network_tools_return_what_they_declare(tools):
    ws, t = tools
    place = t["geocode"](query="Tegernheim, Bayern")
    runs = {
        "geocode": place,
        "osm_features": t["osm_features"](tags={"amenity": "school"}, place="Tegernheim",
                                          output_path="schools.gpkg"),
        "fetch_boundaries": t["fetch_boundaries"](output_path="tegernheim.gpkg",
                                                  match="Tegernheim"),
        "fetch_dem": t["fetch_dem"](bbox=place["bbox"], output_path="dem.tif"),
        "fetch_dgm1": t["fetch_dgm1"](bbox=[12.17, 49.03, 12.18, 49.04],
                                      output_path="dgm1.tif"),
        "geodata_search": t["geodata_search"](query="Stadtbezirke Regensburg"),
        "wfs_capabilities": t["wfs_capabilities"](url=_REGENSBURG_WFS),
        "wfs_features": t["wfs_features"](url=_REGENSBURG_WFS, typename="ms:kleingliederung",
                                          output_path="districts.gpkg"),
    }
    found = [p for name, result in runs.items() for p in problems(name, result)]
    assert not found, found


def test_a_program_may_pass_a_result_field_on_unchanged():
    """CodeMode type-checks the program against the stubs before running it.

    With ``bbox: list[float] | None`` it rejected ``fetch_dem(bbox=place["bbox"])`` twice in
    a row (2026-11-05); with ``wfs_url: NotRequired[str]`` it rejected
    ``wfs_features(url=r.get("wfs_url"))`` (2026-11-07). A field that feeds the next tool
    must type-check as passed on — by subscript and by ``.get``.
    """
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai_harness.code_mode import CodeMode

    program = ('place = await geocode(query="Regensburg")\n'
               'dem = await fetch_dem(bbox=place["bbox"], output_path="dem.tif")\n'
               'info = await vector_info(path=place["boundary"])\n'
               'r = await vector_reproject(input_path=place["boundary"], output_path="b.gpkg",'
               ' target_crs=info["crs"])\n'
               'res = (await geodata_search(query="x"))["candidates"][0]["resources"][0]\n'
               'w = await wfs_features(url=res.get("wfs_url"), typename=res["typename"],'
               ' output_path="d.gpkg")\n'
               '{"n": info["features"] + dem["tiles_used"], "out": r["output"], "w": w["output"]}')
    seen: list = []
    turns = iter([ModelResponse(parts=[ToolCallPart("run_code", {"code": program})]),
                  ModelResponse(parts=[TextPart("done")])])

    def model(messages, _info):
        seen.extend(p.content for p in messages[-1].parts
                    if p.part_kind in ("tool-return", "retry-prompt"))
        return next(turns)

    agent = Agent(FunctionModel(model), capabilities=[CodeMode(
        tools=["geocode", "fetch_dem", "vector_info", "vector_reproject", "geodata_search",
               "wfs_features"])])
    canned = {
        "geocode": {"ok": True, "bbox": [12.0, 49.0, 12.2, 49.1], "boundary": "/ws/b.gpkg"},
        "fetch_dem": {"ok": True, "output": "/ws/dem.tif", "tiles_used": 1},
        "vector_info": {"ok": True, "features": 1, "crs": "EPSG:25832"},
        "vector_reproject": {"ok": True, "output": "/ws/b2.gpkg"},
        "geodata_search": {"ok": True, "candidates": [{"resources": [
            {"service": "WFS", "url": "u", "wfs_url": "w", "typename": "t"}]}]},
        "wfs_features": {"ok": True, "output": "/ws/d.gpkg"},
    }
    for name, value in canned.items():
        def stub(*_a, _v=value, **_k):
            return _v
        stub.__name__ = name
        stub.__doc__ = "Stub."
        agent.tool_plain(toolreturns.typed(_signature_of(name, stub)))
    agent.run_sync("go")
    assert seen and "Type error" not in str(seen[0]), seen[0]


def _signature_of(name: str, stub):
    """Give ``stub`` the real tool's parameters, so the type check sees the real signature."""
    import inspect

    real = {t.__name__: t for t in wrapperlayer.collect_tools("/tmp/chester-sig")}[name]
    stub.__signature__ = inspect.signature(real)
    stub.__annotations__ = dict(real.__annotations__)
    return stub
