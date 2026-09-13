"""`vector_merge` — die zwanzigste geprüfte Operation, und warum es sie gibt.

Gemessen 2026-09-07 (`buffer-schools-500m`, QGIS aus): Der Agent teilte eine gemischte
Schul-Ebene mit `vector_split_by_geometry`, projizierte die drei Teile einzeln mit
`vector_reproject` um — und musste sie dann mit rohem ``pd.concat`` wieder
zusammenlegen, weil es dafür nichts Geprüftes gab. Genau diese eine Datei kam ohne
Provenienz-Sidecar heraus, während die anderen zehn des Laufs einen hatten.

Die Tests messen nach, was ``pd.concat`` mit dem CRS wirklich tut, statt es zu
behaupten: bei zwei verschiedenen bekannten CRS **wirft** es (laut, also harmlos), bei
einer Ebene **ohne** CRS übernimmt es still das Etikett der anderen und lässt die
Koordinaten stehen. Der zweite Fall ist der gefährliche — eine Warnung, die im
Subprozess von `geo_python_run` niemanden erreicht. Und sie halten fest, dass die
Prüfungen am Werkzeug hängen, nicht nur an `geoops`.
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
    """Der Befund des Laufs: Was im Katalog steht, wird benutzt."""
    names = tools_of(VectorCapability(workspace=str(tmp_path)))
    assert "vector_merge" in names
    assert "merge" in geoops.OPERATIONS, "auch im Schnipsel-Namensraum gebunden"


def test_differing_crs_are_reprojected(tmp_path):
    """Wo `pd.concat` abbricht, rechnet `merge` um — und sagt, welche Ebene es traf."""
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
    """Der eigentliche Grund für die Ablehnung, hier nachgemessen statt behauptet.

    `pd.concat` übernimmt das CRS der anderen Ebene und lässt die Koordinaten stehen —
    12.1 Grad steht danach in einer Ebene, die sich EPSG:25832 nennt. Das ist nur eine
    Warnung, und eine Warnung im Subprozess sieht niemand.
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
    """Ohne CRS gibt es nichts, woran ausgerichtet werden könnte — also kein Raten."""
    a = _layer(tmp_path, "with_crs", [Point(0, 0)], crs="EPSG:25832")
    b = _layer(tmp_path, "no_crs", [Point(1, 1)], crs=None)
    out = _merge(tmp_path)(input_paths=[a, b], output_path="merged.gpkg")
    assert out["ok"] is False
    assert "no_crs.gpkg" in out["error"] and "EPSG:25832" in out["error"]


def test_the_output_carries_a_provenance_sidecar(tmp_path):
    """Die eine Datei des Laufs ohne Sidecar war genau die aus `pd.concat`."""
    a = _layer(tmp_path, "a", [Point(0, 0)])
    b = _layer(tmp_path, "b", [Point(1, 1)])
    out = _merge(tmp_path)(input_paths=[a, b], output_path="merged.gpkg")
    import json
    import pathlib

    meta = pathlib.Path(out["output"] + ".meta.json")
    assert meta.exists()
    assert json.loads(meta.read_text())["tool"] == "merge"


def test_a_merge_that_recreates_mixed_geometry_says_so(tmp_path):
    """Punkte und Flächen zusammenlegen heißt: der Split ist rückgängig gemacht."""
    a = _layer(tmp_path, "points", [Point(0, 0)])
    b = _layer(tmp_path, "areas", [box(0, 0, 10, 10)])
    out = _merge(tmp_path)(input_paths=[a, b], output_path="merged.gpkg")
    assert out["ok"] and out["mixed_geometry"] is True
    assert "mixed-geometry" in out["warning"]
    # Und der Header darf den Inhalt nicht belügen — der Grund, aus dem es
    # `vector_split_by_geometry` überhaupt gibt.
    declared = pyogrio.read_info(out["output"])["geometry_type"]
    assert declared in {"Unknown", "GeometryCollection"}, declared


def test_a_uniform_merge_stays_quiet(tmp_path):
    """Kein Warnen auf Vorrat: gleiche Typen, gleiches CRS → nur Zahlen."""
    a = _layer(tmp_path, "a", [Point(0, 0), Point(1, 1)])
    b = _layer(tmp_path, "b", [Point(2, 2)])
    out = _merge(tmp_path)(input_paths=[a, b], output_path="merged.gpkg")
    assert out["ok"] and out["features_in"] == 3 and out["features_out"] == 3
    assert "warning" not in out and "reprojected_to_match" not in out
    assert out["layers"] == [{"path": a, "features": 2}, {"path": b, "features": 1}]


def test_columns_present_in_only_some_layers_are_named(tmp_path):
    """Sonst wundert sich ein späterer Filter über die halb leere Spalte."""
    a = _layer(tmp_path, "a", [Point(0, 0)], name=["x"])
    b = _layer(tmp_path, "b", [Point(1, 1)], height=[12.0])
    out = _merge(tmp_path)(input_paths=[a, b], output_path="merged.gpkg")
    assert sorted(out["columns_only_in_some_layers"]) == ["height", "name"]


def test_one_layer_is_not_a_merge(tmp_path):
    a = _layer(tmp_path, "a", [Point(0, 0)])
    out = _merge(tmp_path)(input_paths=[a], output_path="merged.gpkg")
    assert out["ok"] is False and "at least two" in out["error"]
