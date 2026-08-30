"""Does the data actually cover the area that was asked for? (no network, no QGIS)

A download that reaches over part of the request returns `ok: true` like any other,
and every mean, sum or share computed afterwards is quietly based on the part that
arrived. Li, Ning et al. (2025, Annals of GIS) list exactly this — "Does it
adequately cover the study area?" — among the uncertainties a data-aware system has
to resolve rather than pass on (see `doc/einordnung.md`).

Verified against the real Regensburg DEM before these were written: full raster →
`covers_request: 1.0`, silent; the western half only → `0.427`, and 7 of 18 city
districts flagged, the worst at 1 %.
"""

from __future__ import annotations

import pytest

rasterio = pytest.importorskip("rasterio")

from chester.geofacts import (  # noqa: E402
    coverage_warning,
    raster_coverage,
    zone_coverage,
    zone_coverage_warning,
)

# A 100×100 m raster at 1 m, metric CRS, origin at a round UTM coordinate.
_LEFT, _BOTTOM, _SIZE = 500_000, 5_600_000, 100


def _raster(path, *, hole: bool = False, nodata: float = -9999.0):
    import numpy as np
    from rasterio.transform import from_origin

    data = np.full((_SIZE, _SIZE), 42.0, dtype="float32")
    if hole:
        data[:, : _SIZE // 4] = nodata  # a quarter of the pixels carry nothing
    with rasterio.open(
        path, "w", driver="GTiff", height=_SIZE, width=_SIZE, count=1,
        dtype="float32", crs="EPSG:25832", nodata=nodata,
        transform=from_origin(_LEFT, _BOTTOM + _SIZE, 1, 1),
    ) as dst:
        dst.write(data, 1)
    return str(path)


def _wgs84_bounds(path, scale: float = 1.0):
    """The raster's own bounds in WGS84, optionally widened eastwards."""
    from rasterio.warp import transform_bounds

    with rasterio.open(path) as src:
        left, bottom, right, top = src.bounds
        right = left + (right - left) * scale
        return list(transform_bounds(src.crs, "EPSG:4326", left, bottom, right, top))


def test_a_raster_that_spans_the_request_covers_it_fully(tmp_path):
    path = _raster(tmp_path / "full.tif")
    cov = raster_coverage(path, _wgs84_bounds(path))
    assert cov["covers_request"] == pytest.approx(1.0, abs=0.02)
    assert coverage_warning(cov) is None  # silence when there is nothing to say


def test_a_request_twice_as_wide_is_only_half_covered(tmp_path):
    path = _raster(tmp_path / "half.tif")
    cov = raster_coverage(path, _wgs84_bounds(path, scale=2.0))
    assert cov["covers_request"] == pytest.approx(0.5, abs=0.05)
    assert cov["extent_share"] == pytest.approx(0.5, abs=0.05)
    assert cov["data_share"] == pytest.approx(1.0, abs=0.02)  # no holes, just short
    note = coverage_warning(cov)
    assert "50%" in note and "reaches over only" in note


def test_nodata_inside_the_extent_counts_as_missing(tmp_path):
    """Reaching over the area is not the same as carrying data for it."""
    path = _raster(tmp_path / "holes.tif", hole=True)
    cov = raster_coverage(path, _wgs84_bounds(path))
    assert cov["extent_share"] == pytest.approx(1.0, abs=0.02)
    assert cov["data_share"] == pytest.approx(0.75, abs=0.03)
    assert "nodata" in coverage_warning(cov)


def test_an_unreadable_file_costs_nothing(tmp_path):
    """A coverage read is a nicety — it must never take the download down."""
    assert raster_coverage(str(tmp_path / "missing.tif"), None) is None
    assert coverage_warning(None) is None


def _zones(path, boxes):
    import geopandas as gpd
    from shapely.geometry import box

    gpd.GeoDataFrame(
        {"name": [f"z{i}" for i in range(len(boxes))], "p_count": [b[4] for b in boxes]},
        geometry=[box(*b[:4]) for b in boxes],
        crs="EPSG:25832",
    ).to_file(path)
    return str(path)


def test_a_zone_the_raster_only_half_fills_is_flagged(tmp_path):
    """The pixel count is the only thing that tells a full mean from a partial one."""
    raster = _raster(tmp_path / "r.tif")
    # 50×50 m zones = 2500 pixels each at 1 m. One got them all, one got a fifth.
    zones = _zones(tmp_path / "z.gpkg", [
        (_LEFT, _BOTTOM, _LEFT + 50, _BOTTOM + 50, 2500),
        (_LEFT, _BOTTOM + 50, _LEFT + 50, _BOTTOM + 100, 500),
    ])
    cov = zone_coverage(zones, raster, "p_count")
    assert cov["zones"] == 2
    assert cov["partly_covered_total"] == 1
    assert cov["partly_covered"][0] == {"zone": "z1", "covered": 0.2}
    note = zone_coverage_warning(cov)
    assert "1 of 2 zones" in note and "z1" in note and "20%" in note


def test_zones_in_a_different_crs_are_not_compared(tmp_path):
    """Pixels against square degrees would be a number without a meaning."""
    import geopandas as gpd
    from shapely.geometry import box

    raster = _raster(tmp_path / "r2.tif")
    path = tmp_path / "z4326.gpkg"
    gpd.GeoDataFrame(
        {"name": ["a"], "p_count": [10]},
        geometry=[box(12.0, 49.0, 12.1, 49.1)], crs="EPSG:4326",
    ).to_file(path)
    assert zone_coverage(str(path), raster, "p_count") is None
