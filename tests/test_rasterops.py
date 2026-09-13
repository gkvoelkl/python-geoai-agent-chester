"""Die vier Rasteroperationen auf rasterio (`chester/rasterops.py`) — offline, ohne QGIS.

Phase KQ Schritt 3. Geprueft werden die Fallen, nicht dass rasterio rechnen kann:
nodata darf nicht in eine Statistik einwandern, eine Zone ohne Abdeckung bekommt
`null` statt 0, und eine Aufloesung in Grad wird abgelehnt.
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
    """Der -9999-Fuellwert eines DGM im Mittelwert ist die klassische stille Falschzahl.

    Ohne Maskierung waere der Mittelwert hier rund -2499 statt 3.
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
    """„Kein Wert" und „der Wert ist 0" sind verschiedene Aussagen."""
    src = _raster(tmp_path, "small", [[1, 1], [1, 1]])
    zones = _vector(tmp_path, "two", [box(0, 80, 20, 100), box(5000, 5000, 5020, 5020)])
    res = rasterops.zonal_stats(src, zones, "out.gpkg", stat="mean",
                                workspace=_ws(tmp_path))
    assert res["zones_without_data"] == 1
    assert "null, not 0" in res["warning"]
    got = gpd.read_file(tmp_path / "geocache" / "out.gpkg")
    assert got["mean_value"].isna().sum() == 1


def test_sampling_outside_the_raster_yields_no_value(tmp_path):
    """Ein Punkt ausserhalb bekommt `null` — und die Zahl steht in der Antwort."""
    src = _raster(tmp_path, "grid", [[5, 5], [5, 5]])
    pts = _vector(tmp_path, "pts", [Point(5, 95), Point(9000, 9000)])
    res = rasterops.sample_raster(src, pts, "sampled.gpkg", workspace=_ws(tmp_path))
    assert res["ok"] is True and res["without_value"] == 1
    got = gpd.read_file(tmp_path / "geocache" / "sampled.gpkg")
    assert got["value"].tolist()[0] == 5.0
    assert got["value"].isna().sum() == 1


def test_rasterize_refuses_a_geographic_crs(tmp_path):
    """Eine Aufloesung von 200 in Grad ist keine Zelle von 200 m."""
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
