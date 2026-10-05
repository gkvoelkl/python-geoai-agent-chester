"""The four raster operations on rasterio (`chester/rasterops.py`) — offline, without QGIS.

Phase KQ step 3. The traps are checked, not that rasterio can compute: nodata must not
creep into a statistic, a zone without coverage gets `null` instead of 0, and a
resolution in degrees is refused.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point, box

from chester import rasterops


def _ws(tmp_path):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    return str(tmp_path)


def _raster(tmp_path, name, grid, *, origin=(0.0, 100.0), res=10.0, nodata=None,
            crs="EPSG:25832"):
    _ws(tmp_path)
    path = tmp_path / "geocache" / f"{name}.tif"
    arr = np.asarray(grid, dtype="float32")
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[0],
                       width=arr.shape[1], count=1, dtype="float32", crs=crs,
                       transform=from_origin(origin[0], origin[1], res, res),
                       nodata=nodata) as dst:
        dst.write(arr, 1)
    return f"{name}.tif"


def _vector(tmp_path, name, geoms, crs="EPSG:25832", **cols):
    _ws(tmp_path)
    data = cols or {"x": list(range(len(geoms)))}
    gpd.GeoDataFrame(data, geometry=geoms, crs=crs).to_file(
        tmp_path / "geocache" / f"{name}.gpkg")
    return f"{name}.gpkg"


def test_all_four_operations_are_exported():
    assert set(rasterops.OPERATIONS) == {
        "rasterize", "sample_raster", "zonal_stats", "raster_calc"}


def test_nodata_never_enters_a_statistic(tmp_path):
    """A DEM's -9999 fill value in the mean is the classic silent wrong number.

    Without masking the mean here would be about -2499 instead of 3.
    """
    src = _raster(tmp_path, "dem", [[1, 2], [3, -9999]], nodata=-9999)
    zone = _vector(tmp_path, "zone", [box(0, 80, 20, 100)])
    res = rasterops.zonal_stats(src, zone, "out.gpkg", stat="mean",
                                workspace=_ws(tmp_path))
    assert res["ok"] is True
    got = gpd.read_file(tmp_path / "geocache" / "out.gpkg")
    assert got["mean_value"].iloc[0] == 2.0, "nodata ist eingewandert"
    assert got["coverage"].iloc[0] == 0.75


def test_a_zone_the_raster_does_not_reach_gets_null_not_zero(tmp_path):
    """"No value" and "the value is 0" are different statements."""
    src = _raster(tmp_path, "small", [[1, 1], [1, 1]])
    zones = _vector(tmp_path, "two", [box(0, 80, 20, 100), box(5000, 5000, 5020, 5020)])
    res = rasterops.zonal_stats(src, zones, "out.gpkg", stat="mean",
                                workspace=_ws(tmp_path))
    assert res["zones_without_data"] == 1
    assert "null, not 0" in res["warning"]
    got = gpd.read_file(tmp_path / "geocache" / "out.gpkg")
    assert got["mean_value"].isna().sum() == 1


def test_sampling_outside_the_raster_yields_no_value(tmp_path):
    """A point outside gets `null` — and the count stands in the answer."""
    src = _raster(tmp_path, "grid", [[5, 5], [5, 5]])
    pts = _vector(tmp_path, "pts", [Point(5, 95), Point(9000, 9000)])
    res = rasterops.sample_raster(src, pts, "sampled.gpkg", workspace=_ws(tmp_path))
    assert res["ok"] is True and res["without_value"] == 1
    got = gpd.read_file(tmp_path / "geocache" / "sampled.gpkg")
    assert got["value"].tolist()[0] == 5.0
    assert got["value"].isna().sum() == 1


def test_rasterize_refuses_a_geographic_crs(tmp_path):
    """A resolution of 200 in degrees is not a 200 m cell."""
    v = _vector(tmp_path, "geo", [box(12.0, 49.0, 12.1, 49.1)], crs="EPSG:4326")
    res = rasterops.rasterize(v, "out.tif", resolution=200, workspace=_ws(tmp_path))
    assert res["ok"] is False and "DEGREES" in res["error"]


def test_raster_calc_refuses_grids_that_do_not_line_up(tmp_path):
    """Zwei verschiedene Gitter wuerde numpy stillschweigend broadcasten."""
    a = _raster(tmp_path, "a", [[1, 2], [3, 4]])
    b = _raster(tmp_path, "b", [[1, 2, 3]])
    res = rasterops.raster_calc("out.tif", "a - b", workspace=_ws(tmp_path), a=a, b=b)
    assert res["ok"] is False
    assert "the grids differ" in res["error"] and "broadcast" in res["error"]


def test_raster_calc_computes_and_reports_its_range(tmp_path):
    a = _raster(tmp_path, "a", [[2.0, 4.0]])
    b = _raster(tmp_path, "b", [[1.0, 1.0]])
    res = rasterops.raster_calc("ndx.tif", "(a - b) / (a + b)",
                                workspace=_ws(tmp_path), a=a, b=b)
    assert res["ok"] is True
    lo, hi = res["range"]
    assert round(lo, 4) == 0.3333 and round(hi, 4) == 0.6


def test_rasterize_burns_the_cells_the_geometry_covers(tmp_path):
    """A 100x100 m square at 10 m resolution is exactly 100 cells, each carrying 1.

    The two existing tests check the geographic-CRS refusal and a nodata count; none
    asked whether the right cells get burnt. The count is the whole point of the
    operation — a rasterize that is off by a row still returns `ok: true`.
    """
    v = _vector(tmp_path, "sq", [box(0, 0, 100, 100)])
    res = rasterops.rasterize(v, "burnt.tif", resolution=10.0, workspace=_ws(tmp_path))
    assert res["ok"] is True, res

    with rasterio.open(tmp_path / "geocache" / "burnt.tif") as src:
        arr = src.read(1)
        assert src.res == (10.0, 10.0), src.res
        assert arr.shape == (10, 10), arr.shape
        assert float(np.nansum(arr == 1)) == 100.0, arr


def test_rasterize_burns_the_column_value_not_a_flag(tmp_path):
    """With `column`, each feature carries its own number — 7 over its own cells."""
    v = _vector(tmp_path, "vals", [box(0, 0, 50, 100), box(50, 0, 100, 100)],
                h=[7.0, 3.0])
    res = rasterops.rasterize(v, "vals.tif", resolution=10.0, column="h",
                              workspace=_ws(tmp_path))
    assert res["ok"] is True, res

    with rasterio.open(tmp_path / "geocache" / "vals.tif") as src:
        arr = src.read(1)
    assert float(np.nansum(arr == 7.0)) == 50.0, "the left half is 5x10 cells of 7"
    assert float(np.nansum(arr == 3.0)) == 50.0, "the right half is 5x10 cells of 3"


def test_swapped_zonal_stats_arguments_are_named_not_merely_refused():
    """`zonal_stats(vector, raster)` is the `rasterstats` order — say so.

    Measured 2026-09-27 (`mean-elevation-per-district`): the swapped call opened the
    `.tif` as a vector source and came back with `DataSourceError: not recognized as
    being in a supported file format`. The ressort read that as a broken file, returned
    to hand-rolled rasterio and averaged the nodata zeros outside each polygon into
    eighteen district means — every one of them wrong under `ok: true`.
    """
    res = rasterops.zonal_stats("districts.geojson", "dem.tif", "out.geojson")
    assert res["ok"] is False
    assert "swapped" in res["error"]
    assert "zonal_stats(raster_path, zones_path, output_path)" in res["error"]
    assert "rasterstats" in res["error"], "name the library whose order this is not"


def test_an_unknown_extension_is_never_accused_of_being_swapped():
    """The check fires only when both sides are unambiguous — silence proves nothing."""
    assert rasterops._bad_arguments("dem.tif", "zones.gpkg", "mean") is None
    assert rasterops._bad_arguments("what", "ever", "mean") is None
    assert rasterops._bad_arguments("zones.gpkg", "dem.tif", "mean") is not None


def test_an_unknown_stat_names_the_ones_that_exist():
    res = rasterops.zonal_stats("dem.tif", "zones.gpkg", "out.gpkg", stat="median")
    assert res["ok"] is False
    assert "mean, min, max, sum, count" in res["error"]
