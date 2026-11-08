"""`zonal_stats` reprojects zones to the raster's CRS — the docstring says so, this holds it.

Measured 2026-10-24 (chester-code, `mean-elevation-per-district`): not knowing this, the
model tried to warp the DEM itself (`os.system("gdalwarp …")`, then hand-written rasterio)
and spent three programs on it. Test-Level 1.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box

from chester import rasterops


def test_zones_in_degrees_meet_a_metric_raster(tmp_path):
    dem = tmp_path / "dem.tif"
    with rasterio.open(dem, "w", driver="GTiff", height=100, width=100, count=1,
                       dtype="float32", crs="EPSG:25832",
                       transform=from_origin(720000, 5430000, 10, 10)) as dst:
        dst.write(np.full((100, 100), 350.0, dtype="float32"), 1)
    zone_metric = gpd.GeoSeries([box(720200, 5429200, 720800, 5429800)], crs="EPSG:25832")
    zones = tmp_path / "zones.gpkg"
    gpd.GeoDataFrame({"name": ["a"]}, geometry=zone_metric.to_crs("EPSG:4326")).to_file(zones)

    out = rasterops.zonal_stats(str(dem), str(zones), str(tmp_path / "z.gpkg"),
                                workspace=str(tmp_path))
    assert out.get("ok"), out
    result = gpd.read_file(out["output"])
    value = result.drop(columns=["geometry", "name", "coverage"], errors="ignore").iloc[0]
    assert float(value.iloc[0]) == 350.0
    assert float(result["coverage"].iloc[0]) > 0.9


def test_coverage_counts_the_zone_not_its_bounding_window(tmp_path):
    """Measured 2026-10-24: 18 Regensburg districts on a gapless DEM reported coverage
    0.15–0.71 — the valid cells were divided by the zone's bounding window."""
    dem = tmp_path / "dem.tif"
    with rasterio.open(dem, "w", driver="GTiff", height=100, width=100, count=1,
                       dtype="float32", crs="EPSG:25832",
                       transform=from_origin(720000, 5430000, 10, 10)) as dst:
        dst.write(np.full((100, 100), 350.0, dtype="float32"), 1)
    triangle = Polygon([(720100, 5429100), (720900, 5429100), (720100, 5429900)])
    zones = tmp_path / "zones.gpkg"
    gpd.GeoDataFrame({"name": ["t"]}, geometry=[triangle], crs="EPSG:25832").to_file(zones)

    out = rasterops.zonal_stats(str(dem), str(zones), str(tmp_path / "z.gpkg"),
                                workspace=str(tmp_path))
    assert out.get("ok"), out
    assert float(gpd.read_file(out["output"])["coverage"].iloc[0]) == 1.0
