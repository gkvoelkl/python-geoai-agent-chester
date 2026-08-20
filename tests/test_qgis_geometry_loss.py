"""A QGIS algorithm that drops a geometry type must say so (qgis capability).

`native:clip` and its siblings write **one** geometry type. Handed a layer that
holds several they keep the first and discard the rest without a word — the run
reports `ok: true` and a plausible feature count. Measured 2026-08-19 on
`supermarket-accessibility-choropleth`: 247 OSM supermarkets in (109 points, 138
polygons), 107 out. Because OSM draws the *larger* shops as building polygons,
the survivors were the small ones, and the finished choropleth reported 18
supermarkets for a district that has 80.

Two properties are pinned here, and the second matters as much as the first: the
warning must not fire for an algorithm that changes geometry **by design**.
`buffer` (point→polygon), `centroids` (polygon→point) and `countpointsinpolygon`
(returns the polygons) all "lose" an input type legitimately, and a field that
cries wolf on correct results teaches the model to skip reading it.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from _util import requires_qgis, tools_of
from shapely.geometry import Point, box

from chester.capabilities.qgis import QgisToolboxCapability


@pytest.fixture
def layers(tmp_path):
    """A mixed point+polygon layer, a clean polygon layer, and a clip mask."""
    mixed = gpd.GeoDataFrame(
        {"kind": ["p", "p", "a", "a"]},
        geometry=[Point(10, 10), Point(20, 20),
                  box(30, 30, 40, 40), box(50, 50, 60, 60)],
        crs="EPSG:25832",
    )
    mixed.to_file(tmp_path / "mixed.gpkg", driver="GPKG")
    gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[box(0, 0, 45, 45), box(46, 46, 100, 100)],
        crs="EPSG:25832",
    ).to_file(tmp_path / "areas.gpkg", driver="GPKG")
    gpd.GeoDataFrame(
        {"name": ["mask"]}, geometry=[box(0, 0, 100, 100)], crs="EPSG:25832"
    ).to_file(tmp_path / "mask.gpkg", driver="GPKG")
    return tools_of(QgisToolboxCapability(workspace=str(tmp_path)))


@requires_qgis
def test_a_clip_that_drops_a_geometry_type_says_so(layers, tmp_path):
    result = layers["qgis_clip"](input_path="mixed.gpkg", overlay_path="mask.gpkg",
                                 output_path="clipped.gpkg")
    assert result["ok"]
    warning = result.get("warning") or ""
    # The count of what was lost is the load-bearing part: "some features are
    # missing" is ignorable, "2× Polygon was dropped" is not.
    assert "Polygon" in warning and "dropped" in warning
    assert "centroids" in warning, "der Ausweg muss mitgeliefert werden"


@requires_qgis
def test_a_clean_layer_draws_no_warning(layers, tmp_path):
    result = layers["qgis_clip"](input_path="areas.gpkg", overlay_path="mask.gpkg",
                                 output_path="clean.gpkg")
    assert result["ok"] and "warning" not in result


@requires_qgis
def test_an_algorithm_that_changes_geometry_by_design_draws_no_warning(layers):
    """A buffer turns points into polygons — that is the job, not a loss."""
    result = layers["qgis_buffer"](input_path="areas.gpkg", distance=1,
                                   output_path="buffered.gpkg")
    assert result["ok"] and "warning" not in result


@requires_qgis
def test_count_points_in_polygon_is_not_accused_of_losing_its_input(layers):
    """It returns the POLYGONS layer; every input type is "missing" by design.

    It also counts polygon features, contrary to what its name suggests — checked
    against the same mixed layer, so this test would catch a future QGIS that
    silently stopped doing that.
    """
    result = layers["qgis_run"](
        algorithm_id="native:countpointsinpolygon",
        parameters={"POINTS": "mixed.gpkg", "POLYGONS": "areas.gpkg",
                    "FIELD": "n", "OUTPUT": "counted.gpkg"})
    assert result["ok"], result
    assert "warning" not in result


@requires_qgis
def test_points_and_polygons_resolve_as_paths(layers):
    """`POINTS`/`POLYGONS` were missing from the path-key set.

    Unresolved, qgis_process answered "Could not load source layer for POLYGONS:
    … not found" — a path bug phrased as a missing file, which sent the model
    hunting through `list_directory` for four turns.
    """
    result = layers["qgis_run"](
        algorithm_id="native:countpointsinpolygon",
        parameters={"POINTS": "mixed.gpkg", "POLYGONS": "areas.gpkg",
                    "FIELD": "n", "OUTPUT": "resolved.gpkg"})
    assert result["ok"], result
