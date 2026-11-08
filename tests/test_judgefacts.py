"""`judgefacts.layer_line`: the facts a result-only judge reads, incl. a buffer's radius.

Test-Level 1 — files built in `tmp_path`, no network.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Point, box

from chester import judgefacts


def test_point_buffers_read_as_their_distance(tmp_path):
    """Buffers around points and one school ground: the most frequent radius is the distance."""
    shapes = [Point(721000 + i * 2000, 5428000).buffer(500) for i in range(5)]
    shapes.append(box(730000, 5428000, 730300, 5428300).buffer(500))  # around a ground
    path = tmp_path / "buffers.gpkg"
    gpd.GeoDataFrame(geometry=shapes, crs="EPSG:25832").to_file(path, driver="GPKG")
    line = judgefacts.layer_line(str(path))
    assert line.startswith("buffers.gpkg: EPSG:25832, 6 features, Polygon")
    assert "most frequent ≈ 500 m (5 of 6)" in line


def test_no_radius_for_points_or_degrees(tmp_path):
    pts = tmp_path / "pts.gpkg"
    gpd.GeoDataFrame(geometry=[Point(12.1, 49.0)], crs="EPSG:4326").to_file(pts, driver="GPKG")
    deg = tmp_path / "deg.gpkg"
    gpd.GeoDataFrame(geometry=[Point(12.1, 49.0).buffer(0.01)],
                     crs="EPSG:4326").to_file(deg, driver="GPKG")
    assert "radius" not in judgefacts.layer_line(str(pts))
    assert "radius" not in judgefacts.layer_line(str(deg))
