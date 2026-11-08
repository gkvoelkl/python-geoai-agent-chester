"""A choropleth over a column with one value is drawn plain, in the map and in its picture.

Test-Level 1 — no model, Test-Level-2 fixtures. Found 2026-11-07
(`walk-isochrone-hauptbahnhof`, chester-code, qwen3.6-coder): the isochrone was coloured by
its area column — one feature, one value, one class, the near-white end of YlOrRd. The map
showed a white blot under a blue city boundary, and the answer called the blue the
isochrone. One value is no classification; the layer gets a fixed colour instead, and the
warning names the colours actually drawn.
"""

from __future__ import annotations

from pathlib import Path

from chester import maptools
from chester.mapsnapshot import _render_snapshot

FIXTURES = Path(__file__).parent.parent / "probes" / "fixtures"


def _ws(tmp_path) -> Path:
    """zones: three polygons with three heights; district: one polygon, one height."""
    import geopandas as gpd

    for name, heights in (("zones", [331.0, 352.5, 389.0]), ("district", [340.0])):
        gdf = gpd.read_file(FIXTURES / f"{name}.gpkg")
        gdf["hoehe"] = heights
        gdf.to_file(tmp_path / f"{name}.gpkg")
    return tmp_path


def test_a_single_value_layer_is_drawn_plain_beside_a_real_choropleth(tmp_path):
    ws = _ws(tmp_path)
    (render_map,) = maptools.build_tools(str(ws))
    r = render_map(output_path="m.html", column="hoehe",
                   layers=[str(ws / "zones.gpkg"), str(ws / "district.gpkg")])
    assert r["ok"], r
    assert r["choropleth"]["column"] == "hoehe"  # zones are classified
    colours = {Path(k).name: v["colour"] for k, v in r["styling"].items()}
    assert colours["zones.gpkg"].startswith("choropleth")
    assert not colours["district.gpkg"].startswith("choropleth")  # one value → plain


def test_a_map_whose_only_column_is_flat_says_what_it_drew(tmp_path):
    ws = _ws(tmp_path)
    (render_map,) = maptools.build_tools(str(ws))
    r = render_map(output_path="m.html", column="hoehe", layers=[str(ws / "district.gpkg")])
    assert r["ok"], r
    assert "choropleth" not in r
    assert "one value" in r["warning"] and "district.gpkg=" in r["warning"]


def test_the_picture_follows_the_map(tmp_path):
    """The vision check looks at the PNG — it must show what the HTML shows."""
    ws = _ws(tmp_path)
    _png, summary = _render_snapshot([str(ws / "zones.gpkg"), str(ws / "district.gpkg")],
                                     str(ws), "hoehe", "NaturalBreaks", 5, "YlOrRd", "t")
    by_layer = {s["layer"]: s for s in summary}
    assert by_layer["zones.gpkg"]["colour"].startswith("shaded by hoehe")
    assert not str(by_layer["district.gpkg"]["colour"]).startswith("shaded")
