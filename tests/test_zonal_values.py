"""`zonal_stats` returns each zone's number, not only the path of the file holding it.

Test-Level 1, no network. Found 2026-11-07 (`mean-elevation-per-district`, chester-code):
the means were right in the file, but the coder could not get them into its program —
nested f-strings in `geo_python_run`, then `sqlite3`, then a `geopandas` import the
sandbox refuses; after 15 min and 14 programs the run was stopped.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_bounds

from chester import rasterops

FIXTURES = Path(__file__).parent.parent / "probes" / "fixtures"


def _raster_over_zones(ws: Path) -> Path:
    """A 1-m raster over the zones whose value is its column index — every zone differs."""
    zones = gpd.read_file(ws / "zones.gpkg")
    west, south, east, north = zones.total_bounds
    width, height = int(east - west) + 1, int(north - south) + 1
    data = np.tile(np.arange(width, dtype="float32"), (height, 1))
    path = ws / "grid.tif"
    with rasterio.open(path, "w", driver="GTiff", width=width, height=height, count=1,
                       dtype="float32", crs=zones.crs,
                       transform=from_bounds(west, south, west + width, south + height,
                                             width, height)) as dst:
        dst.write(data, 1)
    return path


def test_each_zone_comes_back_by_name_with_its_value(tmp_path):
    shutil.copy(FIXTURES / "zones.gpkg", tmp_path / "zones.gpkg")
    grid = _raster_over_zones(tmp_path)
    r = rasterops.zonal_stats(str(grid), str(tmp_path / "zones.gpkg"), "z.gpkg",
                              workspace=str(tmp_path))
    assert r["ok"], r
    by_zone = {v["zone"]: v for v in r["values"]}
    assert set(by_zone) == {"Z1", "Z2", "Z3"}
    written = gpd.read_file(r["output"]).set_index("name")[r["column"]]
    for name, v in by_zone.items():
        assert v["coverage"] == 1.0
        assert v["value"] == round(float(written[name]), 3)  # the file and the list agree


def test_a_zone_the_raster_misses_is_null_in_the_list_too(tmp_path):
    for name in ("zones.gpkg", "aerial_rgb.tif"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    r = rasterops.zonal_stats(str(tmp_path / "aerial_rgb.tif"), str(tmp_path / "zones.gpkg"),
                              "z.gpkg", workspace=str(tmp_path))
    assert [v["value"] for v in r["values"]] == [None, None, None]
    assert [v["zone"] for v in r["values"]] == ["Z1", "Z2", "Z3"]
