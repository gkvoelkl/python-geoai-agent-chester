"""`vector_merge` — the twentieth checked operation, and why it exists.

Measured 2026-09-07 (`buffer-schools-500m`, QGIS off): the agent split a mixed school
layer with `vector_split_by_geometry`, reprojected the three parts one by one with
`vector_reproject` — and then had to put them back together with raw ``pd.concat``,
because there was nothing checked for it. Exactly that one file came out without a
provenance sidecar, while the other ten of the run had one.

The tests measure what ``pd.concat`` really does with the CRS instead of claiming it:
with two different known CRS it **raises** (loud, so harmless), with a layer **without**
a CRS it silently takes over the other's label and leaves the coordinates as they are.
The second case is the dangerous one — a warning that reaches nobody in the
`geo_python_run` subprocess. And they pin that the checks hang on the tool, not only on
`geoops`.
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
from _util import tools_of
from shapely.geometry import Point, box

from chester import geoops
from chester.capabilities.vector import VectorCapability


def _layer(tmp_path, stem, geoms, crs="EPSG:25832", **columns):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    gdf = gpd.GeoDataFrame({**columns, "geometry": geoms}, crs=crs)
    gdf.to_file(tmp_path / "geocache" / f"{stem}.gpkg")
    return f"{stem}.gpkg"


def _merge(tmp_path):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    return tools_of(VectorCapability(workspace=str(tmp_path)))["vector_merge"]


def test_vector_merge_is_a_tool(tmp_path):
    """The run's finding: what is in the catalogue gets used."""
    names = tools_of(VectorCapability(workspace=str(tmp_path)))
    assert "vector_merge" in names
    assert "merge" in geoops.OPERATIONS, "auch im Schnipsel-Namensraum gebunden"


def test_differing_crs_are_reprojected(tmp_path):
    """Where `pd.concat` gives up, `merge` reprojects — and says which layer it hit."""
    a = _layer(tmp_path, "a", [Point(4500000, 5430000)], crs="EPSG:25832")
    b = _layer(tmp_path, "b", [Point(12.1, 49.02)], crs="EPSG:4326")

    with pytest.raises(ValueError, match="common CRS"):
        pd.concat([gpd.read_file(tmp_path / "geocache" / "a.gpkg"),
                   gpd.read_file(tmp_path / "geocache" / "b.gpkg")], ignore_index=True)

    out = _merge(tmp_path)(input_paths=[a, b], output_path="merged.gpkg")
    assert out["ok"] and out["features_out"] == 2
    assert out["crs"] == "EPSG:25832"
    assert out["reprojected_to_match"] == [b], "die Umprojektion wird berichtet"
    merged = gpd.read_file(out["output"])
    assert merged.geometry.iloc[1].x > 100000, "umgerechnet, nicht nur umbenannt"


def test_the_silent_case_is_a_layer_without_crs(tmp_path):
    """The actual reason for refusing, measured here rather than claimed.

    `pd.concat` takes over the other layer's CRS and leaves the coordinates — 12.1 degrees
    then stands in a layer that calls itself EPSG:25832. That is only a warning, and
    nobody sees a warning in a subprocess.
    """
    a = gpd.GeoDataFrame({"geometry": [Point(4500000, 5430000)]}, crs="EPSG:25832")
    b = gpd.GeoDataFrame({"geometry": [Point(12.1, 49.02)]}, crs=None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        naive = pd.concat([a, b], ignore_index=True)
    assert str(naive.crs) == "EPSG:25832"
    assert naive.geometry.iloc[1].x == pytest.approx(12.1), "Grad in einer Meter-Ebene"
    assert any("CRS not set" in str(c.message) for c in caught), "nur eine Warnung"


def test_a_layer_without_crs_is_refused(tmp_path):
    """Without a CRS there is nothing to align to — so no guessing."""
    a = _layer(tmp_path, "with_crs", [Point(0, 0)], crs="EPSG:25832")
    b = _layer(tmp_path, "no_crs", [Point(1, 1)], crs=None)
    out = _merge(tmp_path)(input_paths=[a, b], output_path="merged.gpkg")
    assert out["ok"] is False
    assert "no_crs.gpkg" in out["error"] and "EPSG:25832" in out["error"]


def test_the_output_carries_a_provenance_sidecar(tmp_path):
    """The one file of the run without a sidecar was exactly the one from `pd.concat`."""
    a = _layer(tmp_path, "a", [Point(0, 0)])
    b = _layer(tmp_path, "b", [Point(1, 1)])
    out = _merge(tmp_path)(input_paths=[a, b], output_path="merged.gpkg")
    import json
    import pathlib

    meta = pathlib.Path(out["output"] + ".meta.json")
    assert meta.exists()
    assert json.loads(meta.read_text())["tool"] == "merge"


def test_a_merge_that_recreates_mixed_geometry_says_so(tmp_path):
    """Merging points and polygons means: the split has been undone."""
    a = _layer(tmp_path, "points", [Point(0, 0)])
    b = _layer(tmp_path, "areas", [box(0, 0, 10, 10)])
    out = _merge(tmp_path)(input_paths=[a, b], output_path="merged.gpkg")
    assert out["ok"] and out["mixed_geometry"] is True
    assert "mixed-geometry" in out["warning"]
    # And the header must not lie about the content — the reason
    # `vector_split_by_geometry` exists at all.
    declared = pyogrio.read_info(out["output"])["geometry_type"]
    assert declared in {"Unknown", "GeometryCollection"}, declared


def test_a_uniform_merge_stays_quiet(tmp_path):
    """No warning just in case: same types, same CRS → only numbers."""
    a = _layer(tmp_path, "a", [Point(0, 0), Point(1, 1)])
    b = _layer(tmp_path, "b", [Point(2, 2)])
    out = _merge(tmp_path)(input_paths=[a, b], output_path="merged.gpkg")
    assert out["ok"] and out["features_in"] == 3 and out["features_out"] == 3
    assert "warning" not in out and "reprojected_to_match" not in out
    assert out["layers"] == [{"path": a, "features": 2}, {"path": b, "features": 1}]


def test_columns_present_in_only_some_layers_are_named(tmp_path):
    """Otherwise a later filter wonders about the half-empty column."""
    a = _layer(tmp_path, "a", [Point(0, 0)], name=["x"])
    b = _layer(tmp_path, "b", [Point(1, 1)], height=[12.0])
    out = _merge(tmp_path)(input_paths=[a, b], output_path="merged.gpkg")
    assert sorted(out["columns_only_in_some_layers"]) == ["height", "name"]


def test_one_layer_is_not_a_merge(tmp_path):
    a = _layer(tmp_path, "a", [Point(0, 0)])
    out = _merge(tmp_path)(input_paths=[a], output_path="merged.gpkg")
    assert out["ok"] is False and "at least two" in out["error"]
