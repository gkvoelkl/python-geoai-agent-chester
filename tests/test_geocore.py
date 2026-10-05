"""GeoCoreCapability — raster, terrain and network as **tools** (Phase KQ).

Measured 2026-09-07 (`buffer-schools-500m`, QGIS off): the agent called
`vector_split_by_geometry` unprompted — a tool — and did not touch the same operations in
the sandbox namespace a single time ("Since I can't call 'reproject' inside here"). What
is in the tool catalogue gets used; what is only in the prose does not. These tests pin
that the traps travel with the tool — not only with the function behind it.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import rasterio
from _util import tools_of
from rasterio.transform import from_origin
from shapely.geometry import Point, box

from chester.capabilities.geocore import GeoCoreCapability


def _tools(tmp_path):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    return tools_of(GeoCoreCapability(workspace=str(tmp_path)))


def _raster(tmp_path, name, grid, nodata=None, crs="EPSG:25832"):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    arr = np.asarray(grid, dtype="float32")
    with rasterio.open(tmp_path / "geocache" / f"{name}.tif", "w", driver="GTiff",
                       height=arr.shape[0], width=arr.shape[1], count=1,
                       dtype="float32", crs=crs,
                       transform=from_origin(0, 100, 10, 10), nodata=nodata) as dst:
        dst.write(arr, 1)
    return f"{name}.tif"


def _vector(tmp_path, name, geoms, crs="EPSG:25832"):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame({"x": list(range(len(geoms)))}, geometry=geoms, crs=crs).to_file(
        tmp_path / "geocache" / f"{name}.gpkg")
    return f"{name}.gpkg"


def test_all_eleven_are_tools(tmp_path):
    """Eleven operations, eleven tools — with the same names as in the namespace."""
    tools = _tools(tmp_path)
    assert set(tools) == {
        "rasterize", "sample_raster", "zonal_stats", "raster_calc",
        "slope", "aspect", "hillshade", "ruggedness", "fill_sinks",
        "flow_accumulation", "service_area"}


def test_nodata_never_enters_a_statistic_through_the_tool(tmp_path):
    """The trap travels with the tool, not only with `rasterops.zonal_stats`."""
    tools = _tools(tmp_path)
    src = _raster(tmp_path, "dem", [[1, 2], [3, -9999]], nodata=-9999)
    zone = _vector(tmp_path, "zone", [box(0, 80, 20, 100)])
    res = tools["zonal_stats"](raster_path=src, zones_path=zone,
                               output_path="out.gpkg", stat="mean")
    assert res["ok"] is True
    got = gpd.read_file(tmp_path / "geocache" / "out.gpkg")
    assert got["mean_value"].iloc[0] == 2.0, "nodata ist eingewandert"
    assert got["coverage"].iloc[0] == 0.75


def test_a_point_outside_the_raster_gets_null_through_the_tool(tmp_path):
    """"No value" and "the value is 0" stay different statements."""
    tools = _tools(tmp_path)
    src = _raster(tmp_path, "grid", [[5, 5], [5, 5]])
    pts = _vector(tmp_path, "pts", [Point(5, 95), Point(9000, 9000)])
    res = tools["sample_raster"](raster_path=src, points_path=pts,
                                 output_path="s.gpkg")
    assert res["ok"] is True and res["without_value"] == 1


def test_rasterize_refuses_degrees_through_the_tool(tmp_path):
    tools = _tools(tmp_path)
    v = _vector(tmp_path, "geo", [box(12.0, 49.0, 12.1, 49.1)], crs="EPSG:4326")
    res = tools["rasterize"](vector_path=v, output_path="o.tif", resolution=200)
    assert res["ok"] is False and "DEGREES" in res["error"]


def test_hillshade_says_it_is_a_picture(tmp_path):
    """A hillshade looks like terrain data and carries none."""
    tools = _tools(tmp_path)
    dem = _raster(tmp_path, "d", [[10, 20, 30], [10, 20, 30], [10, 20, 30]])
    res = tools["hillshade"](dem_path=dem, output_path="hs.tif")
    assert res["ok"] is True and "not a measurement" in res["note"]


def test_service_area_refuses_a_start_off_the_network(tmp_path):
    """Otherwise the isochrone describes a different place than the one asked for."""
    from shapely.geometry import LineString

    tools = _tools(tmp_path)
    net = _vector(tmp_path, "net", [LineString([(0, 0), (100, 0)])])
    res = tools["service_area"](network_path=net, output_path="iso.gpkg",
                                start_lon=900000.0, start_lat=5400000.0,
                                minutes=10, start_crs="EPSG:25832")
    assert res["ok"] is False and "from the start point" in res["error"]
