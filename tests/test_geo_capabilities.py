"""Local geo capability tests (geopandas/rasterio; no QGIS, no network)."""

from _util import (
    tools_of,
    write_bands,
    write_building_sample,
    write_point,
)

from chester.capabilities.mapoutput import MapOutputCapability
from chester.capabilities.perception import PerceptionCapability
from chester.capabilities.validation import GeoValidationCapability
from chester.capabilities.vector import VectorCapability

# ── vector ──────────────────────────────────────────────────────────────────


def test_vector_info_reports_schema(tmp_path):
    sample = write_building_sample(tmp_path)
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    r = tools["vector_info"](path=str(sample["buildings"]))
    assert r["ok"] and r["features"] == 3
    assert r["geometry_types"] == ["Polygon"]
    assert "true_height" in r["columns"]


def test_vector_info_lists_a_columns_values(tmp_path):
    # "Which of these features is the one I mean?" — asked five times as a PyQGIS
    # snippet in one benchmark run because no tool answered it.
    sample = write_building_sample(tmp_path)
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    r = tools["vector_info"](path=str(sample["buildings"]), values_of="name")
    assert r["ok"]
    assert r["values"]["values"] == ["Klein", "Mittel", "Hoch"], "Reihenfolge der Ebene"
    assert r["values"]["distinct"] == 3 and r["values"]["truncated"] is False


def test_vector_info_without_values_of_stays_unchanged(tmp_path):
    sample = write_building_sample(tmp_path)
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    assert "values" not in tools["vector_info"](path=str(sample["buildings"]))


def test_vector_info_names_the_columns_when_the_asked_one_is_missing(tmp_path):
    # Same failure style as vector_filter: answer with what exists, don't just say no.
    sample = write_building_sample(tmp_path)
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    r = tools["vector_info"](path=str(sample["buildings"]), values_of="bezirk")
    assert r["ok"], "eine fehlende Spalte macht die Layer-Auskunft nicht ungültig"
    assert "error" in r["values"] and "true_height" in r["values"]["available_columns"]


def test_vector_filter_keeps_matching(tmp_path):
    sample = write_building_sample(tmp_path)
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    r = tools["vector_filter"](
        path=str(sample["buildings"]),
        expression="true_height > 15",
        output_path="tall.geojson",
    )
    assert r["ok"] and r["before"] == 3 and r["after"] == 2


def test_vector_filter_names_the_sql_mistake_instead_of_the_quoting_rule(tmp_path):
    """Ein SQL-Ausdruck muss als SQL-Ausdruck gemeldet werden, nicht als Quoting-Frage.

    Anlass (2026-09-03, `laguna-xs-2.1` auf `pluvial-flow-accumulation-tegernheim`):
    Das Modell schrieb `"waterway" IN ('stream', …) AND geometry IS NOT NULL`, bekam
    einen SyntaxError samt Hinweis auf Anführungszeichen und Backticks, befolgte den
    Hinweis, scheiterte erneut — und wich danach auf handgeschriebenes PyQGIS aus.
    Der alte Hinweis war nicht falsch, er passte nur nicht zum Fehler, und das kostet
    mehr als gar keiner.
    """
    sample = write_building_sample(tmp_path)
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    r = tools["vector_filter"](
        path=str(sample["buildings"]),
        expression="\"true_height\" IN (15, 20) AND geometry IS NOT NULL",
        output_path="x.geojson",
    )

    assert r["ok"] is False
    assert "pandas" in r["hint"] and "IN (…)" in r["hint"]
    assert "and" in r["hint"] and "AND" in r["hint"]  # die konkrete Stelle, nicht die Regel
    # Die zwei Werkzeuge, die SQL-nahe Ausdrücke wirklich annehmen.
    assert "qgis_extract_by_attribute" in r["hint"]
    assert "native:extractbyexpression" in r["hint"]


def test_vector_filter_keeps_the_quoting_hint_where_it_fits(tmp_path):
    """Ohne SQL-Merkmale bleibt der alte Hinweis — er war für seinen Fall richtig."""
    sample = write_building_sample(tmp_path)
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    r = tools["vector_filter"](
        path=str(sample["buildings"]), expression="true_height >>> 3", output_path="x.geojson"
    )

    assert r["ok"] is False
    assert "backticks" in r["hint"] and "pandas" not in r["hint"]


def test_vector_filter_lists_columns_only_when_one_might_be_missing(tmp_path):
    """Bei einem Syntaxfehler hilft die Spaltenliste nicht — sie füllt nur den Kontext.

    Im auslösenden Fall kamen 40 OSM-Attributnamen wie
    `TMC:cid_58:tabcd_1:LocationCode` zurück, während der Fehler ein Syntaxfehler war.
    """
    sample = write_building_sample(tmp_path)
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))

    syntax = tools["vector_filter"](
        path=str(sample["buildings"]), expression="true_height >>> 3", output_path="a.geojson"
    )
    unknown = tools["vector_filter"](
        path=str(sample["buildings"]), expression="gibtsnicht > 1", output_path="b.geojson"
    )

    assert "available_columns" not in syntax, "Syntaxfehler braucht keine Spaltenliste"
    assert "true_height" in unknown.get("available_columns", []), "hier hilft sie"


def test_vector_filter_empty_match_is_not_ok(tmp_path):
    sample = write_building_sample(tmp_path)
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    r = tools["vector_filter"](
        path=str(sample["buildings"]),
        expression="true_height > 1000",
        output_path="none.geojson",
    )
    assert r["ok"] is False


def _write_osm_like(path):
    """A small OSM-style layer: colon column names + mostly-empty tag columns."""
    import geopandas as gpd
    from shapely.geometry import Point

    gdf = gpd.GeoDataFrame(
        {
            "building": ["yes", "detached", "detached"],
            "addr:street": [None, "Hollerweg", "Hollerweg"],
            "addr:streetnumber": [None, "24", "7"],
            "empty_tag": [None, None, None],
            "geometry": [Point(0, 0), Point(1, 1), Point(2, 2)],
        },
        crs="EPSG:4326",
    )
    gdf.to_file(path, driver="GeoJSON")


def test_vector_filter_handles_osm_colon_columns(tmp_path):
    # The colon in `addr:street` used to break pandas .query(); it must now
    # work without the model adding backticks itself.
    _write_osm_like(tmp_path / "osm.geojson")
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    r = tools["vector_filter"](
        path="osm.geojson",
        expression="addr:street == 'Hollerweg'",
        output_path="hollerweg.geojson",
    )
    assert r["ok"] and r["before"] == 3 and r["after"] == 2


def test_vector_info_lists_only_populated_columns(tmp_path):
    _write_osm_like(tmp_path / "osm.geojson")
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    r = tools["vector_info"](path="osm.geojson")
    assert r["ok"]
    assert "empty_tag" not in r["columns"]  # all-null column hidden
    assert "addr:street" in r["columns"]
    assert r["columns_empty"] >= 1


def test_osm_apply_where_filters_and_reports_missing(tmp_path):
    import geopandas as gpd

    from chester.capabilities.discovery import _apply_where

    _write_osm_like(tmp_path / "osm.geojson")
    gdf = gpd.read_file(tmp_path / "osm.geojson")

    filt, missing = _apply_where(gdf, {"addr:street": "hollerweg"})  # case-insensitive
    assert not missing and len(filt) == 2

    _, missing2 = _apply_where(gdf, {"addr:nope": "x"})
    assert missing2 == ["addr:nope"]


# ── validation ──────────────────────────────────────────────────────────────


def test_check_crs_flags_geographic(tmp_path):
    pt = write_point(tmp_path / "g.geojson", 7.1, 50.7, "EPSG:4326")
    tools = tools_of(GeoValidationCapability(workspace=str(tmp_path)))
    r = tools["check_crs"](path=str(pt))
    assert r["is_geographic"] is True
    assert r["ok"] is False  # not safe for measurement


def test_check_crs_accepts_projected(tmp_path):
    pt = write_point(tmp_path / "p.geojson", 500000, 5600000, "EPSG:25832")
    tools = tools_of(GeoValidationCapability(workspace=str(tmp_path)))
    r = tools["check_crs"](path=str(pt))
    assert r["ok"] is True and r["is_geographic"] is False


def test_sanity_check_result(tmp_path):
    sample = write_building_sample(tmp_path)
    tools = tools_of(GeoValidationCapability(workspace=str(tmp_path)))
    r = tools["sanity_check_result"](path=str(sample["buildings"]), expected_geometry="Polygon")
    assert r["ok"] and r["features"] == 3 and r["warnings"] == []


# ── perception ──────────────────────────────────────────────────────────────


def test_detect_water_recovers_block_area(tmp_path):
    green = tmp_path / "green.tif"
    nir = tmp_path / "nir.tif"
    write_bands(green, nir)
    tools = tools_of(PerceptionCapability(workspace=str(tmp_path)))
    r = tools["detect_water"](
        green=str(green),
        nir=str(nir),
        mask_path="mask.tif",
        threshold=0.0,
        polygons_path="water.geojson",
    )
    assert r["ok"] and r["polygon_count"] == 1

    import geopandas as gpd

    # Output is confined to geocache/; read it back from the returned path.
    area = gpd.read_file(r["polygons"]).geometry.area.sum()
    assert abs(area - 400.0) < 1.0  # the 20x20 m water block


# ── map output ──────────────────────────────────────────────────────────────


def test_render_map_writes_leaflet_html(tmp_path):
    import os
    from pathlib import Path

    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](layers=[str(sample["buildings"])], output_path="map.html")
    assert r["ok"]
    assert "leaflet" in Path(r["output"]).read_text().lower()
    # The returned path must be absolute and exist — the dashboard embeds the map
    # via os.path.isfile(<path from the reply>), so a relative path would miss.
    assert os.path.isabs(r["output"]) and os.path.isfile(r["output"])


def test_render_map_size_guard_reports_failure_not_success(tmp_path, monkeypatch):
    """A guard that writes nothing must not answer `ok: true`.

    From `dem-contours-10m` (2026-08-25): 19 486 contours rendered to 87 MB, over the
    45 MB cap, so the HTML was written and deleted again — and the tool still said
    `ok: true`. Nothing at output_path, no `output` key, so the validation gate saw
    no artefact either. In an earlier run the same shape produced an answer that
    linked a map and pre-excused its absence ("liegt an der Dateigröße").
    """
    from chester.capabilities import mapoutput

    sample = write_building_sample(tmp_path)
    monkeypatch.setattr(mapoutput, "_MAX_INLINE_MB", 1e-9)  # force the size backstop
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](layers=[str(sample["buildings"])], output_path="big.html")

    assert r["ok"] is False and r["embedded"] is False
    assert "output" not in r and "picture" not in r  # nothing to quote
    assert "NO file was written" in r["reason"]
    assert r["recommend_tool"] == "qgis_show"
    assert not (tmp_path / "geocache" / "big.html").exists()


def test_render_map_feature_guard_reports_failure_not_success(tmp_path, monkeypatch):
    """The cheap pre-check ahead of the render has the same contract."""
    from chester.capabilities import mapoutput

    sample = write_building_sample(tmp_path)
    monkeypatch.setattr(mapoutput, "_MAX_INLINE_FEATURES", 0)  # force the pre-check
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](layers=[str(sample["buildings"])], output_path="big.html")

    assert r["ok"] is False and r["embedded"] is False
    assert "output" not in r
    assert "NO file was written" in r["reason"]


def test_render_map_vertex_guard_falls_back_to_the_picture(tmp_path, monkeypatch):
    """Zu viele Stützpunkte → das Bild ist das Ergebnis, nicht das Nichts.

    Anlass: 6.888 Höhenlinien fielen durch **beide** alten Wächter — weit unter der
    50.000-Objekt-Grenze, mit 42,5 MB knapp unter dem 45-MB-Deckel — und die HTML
    blieb im Browser weiss (2026-09-02, `pluvial-flow-accumulation-tegernheim`).
    Gemessen waren es 936.687 Stützpunkte; die entscheiden über die Renderlast,
    nicht die Objektzahl und nicht die Bytes.

    Anders als bei den beiden anderen Wächtern ist das Ergebnis hier **kein**
    Fehlschlag: Dasselbe Kartenbild liegt als PNG vor, und ein PNG ist genau das,
    was ein Leser bei zu vielen Linien braucht. Es als `ok: false` zu melden hiesse,
    ein vorhandenes Ergebnis zu verschweigen.
    """
    from pathlib import Path

    from chester.capabilities import mapoutput

    sample = write_building_sample(tmp_path)
    monkeypatch.setattr(mapoutput, "_MAX_INLINE_VERTICES", 0)  # Wächter erzwingen
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](layers=[str(sample["buildings"])], output_path="dense.html")

    assert r["ok"] is True and r["embedded"] is False
    assert r["output"] == r["picture"], "das Bild IST die Ausgabe, kein Anhang"
    assert r["output"].endswith(".png")
    assert Path(r["output"]).is_file()
    assert not (tmp_path / "geocache" / "dense.html").exists(), "die HTML muss weg sein"
    assert r["vertices"] > 0
    # Der Ausweg muss im Rückgabewert stehen, nicht nur in der Instruktion — das
    # ist in diesem Projekt der Kanal, der Verhalten dreht.
    assert "STATIC PICTURE" in r["reason"] and "report its path" in r["reason"]


def test_render_map_vertex_guard_counts_real_geometry(tmp_path):
    """Der Zähler muss Stützpunkte zählen, nicht Objekte — sonst misst er das Falsche."""
    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](layers=[str(sample["buildings"])], output_path="ok.html")

    assert r["ok"] is True
    # Unter der Grenze faellt der Waechter nicht auf: kein `vertices`, kein `reason`.
    assert "vertices" not in r and "reason" not in r
    assert (tmp_path / "geocache" / "ok.html").is_file()


def test_render_map_basemap_selects_tiles(tmp_path):
    from pathlib import Path

    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](
        layers=[str(sample["buildings"])],
        output_path="m.html",
        basemap="CartoDB positron",
    )
    html = Path(r["output"]).read_text()
    assert "cartocdn" in html and "tile.openstreetmap.org" not in html


def test_render_map_wms_overlay_embeds_service(tmp_path):
    from pathlib import Path

    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](
        layers=[str(sample["buildings"])],
        output_path="wms.html",
        wms_url="https://example.org/wms",
        wms_layer="test:layer",
        wms_attribution="© Testdienst",
    )
    assert r["ok"] and r["wms"] == {"url": "https://example.org/wms", "layer": "test:layer"}
    html = Path(r["output"]).read_text()
    # folium WmsTileLayer wires the service into the page without any request.
    assert "example.org/wms" in html and "© Testdienst" in html


def test_render_map_no_layers_no_wms_errors(tmp_path):
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](output_path="empty.html")
    assert not r["ok"] and "no layers" in r["error"]


def test_render_map_choropleth_classifies_by_column(tmp_path):
    from pathlib import Path

    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](
        layers=[str(sample["buildings"])],
        output_path="choro.html",
        column="true_height",
        scheme="NaturalBreaks",
        cmap="YlOrRd",
    )
    assert r["ok"]
    # The choropleth metadata is echoed, and k is clamped to the distinct values
    # present (3 heights) even though the default k is 5.
    assert r["choropleth"]["column"] == "true_height"
    assert r["choropleth"]["k"] == 3
    html = Path(r["output"]).read_text()
    assert "leaflet" in html.lower()


def test_render_map_choropleth_missing_column_errors(tmp_path):
    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](
        layers=[str(sample["buildings"])],
        output_path="x.html",
        column="nope",
    )
    assert r["ok"] is False and "not found" in r["error"]
    # The error is actionable: it lists the real columns and points display
    # fields to `fields`, so the model can recover in one step.
    assert "true_height" in r["error"] and "fields=" in r["error"]


def _write_point_layer(out_dir, name, n):
    import geopandas as gpd
    from shapely.geometry import Point

    path = out_dir / name
    gpd.GeoDataFrame(
        {"i": list(range(n))},
        geometry=[Point(500000 + i, 5600000 + i) for i in range(n)],
        crs="EPSG:25832",
    ).to_file(path)
    return path


def test_render_map_reports_the_colours_it_really_used(tmp_path):
    """Without `column`, cmap/scheme/k do nothing — and the answer must know.

    From `road-impact-greenspace-100m` (2026-08-26): the agent passed
    cmap="Greens" and then described "hellgrün" total greenery and "dunkelgrün"
    affected areas. `_COLORS` had drawn them blue and orange, the roads green.
    The model cannot see the map, so a return value that stays silent about the
    palette is an invitation to invent one.
    """
    sample = write_building_sample(tmp_path)
    points = _write_point_layer(tmp_path, "nodes.geojson", 3)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](
        layers=[str(sample["buildings"]), str(points)],
        output_path="stack.html",
        cmap="Greens",  # ignored without a column — that is the point
    )
    assert r["ok"]
    colours = [s["colour"] for s in r["styling"].values()]
    assert colours == ["#3388ff", "#e6550d"]  # palette order, not "Greens"
    assert "had NO effect" in r["warning"]
    assert "#e6550d" in r["warning"] and "drawn on top" in r["warning"]


def test_render_map_choropleth_does_not_claim_the_palette_was_ignored(tmp_path):
    """The mirror case: with a column the cmap *does* apply, so no warning."""
    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](
        layers=[str(sample["buildings"])],
        output_path="choro2.html",
        column="true_height",
        cmap="Greens",
    )
    assert r["ok"]
    assert "warning" not in r
    assert r["styling"][str(sample["buildings"])]["colour"] == "choropleth(Greens)"


def test_render_map_shrinks_markers_on_a_point_heavy_layer(tmp_path):
    """4585 road nodes at Folium's default 10 px radius buried the answer layer.

    Same run: the affected green areas were drawn *below* a layer of OSM highway
    nodes and simply not visible. The count belongs in the return value too — a
    "roads" layer that is one third points is worth saying out loud.
    """
    from pathlib import Path

    points = _write_point_layer(tmp_path, "many.geojson", 600)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](layers=[str(points)], output_path="dots.html")
    assert r["ok"]
    assert r["styling"][str(points)]["points_as_markers"] == 600
    assert '"radius": 3' in Path(r["output"]).read_text()


def test_render_map_comma_joined_columns_routes_to_fields(tmp_path):
    """`columns="name,true_height"` — the model's comma-joined display-field
    string — must render (routed to popup `fields`), not error as a bogus
    single choropleth column named "name,true_height"."""
    from pathlib import Path

    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](
        layers=[str(sample["buildings"])],
        output_path="cj.html",
        columns="name,true_height",
    )
    assert r["ok"], r.get("error")
    # Routed to fields → no choropleth was applied, and both names ride along.
    assert "choropleth" not in r
    html = Path(r["output"]).read_text()
    assert "true_height" in html and "name" in html


def test_render_map_single_columns_alias_is_choropleth(tmp_path):
    """A single `columns` value is still the choropleth column (back-compat)."""
    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](
        layers=[str(sample["buildings"])],
        output_path="sc.html",
        columns="true_height",
    )
    assert r["ok"], r.get("error")
    assert r["choropleth"]["column"] == "true_height"


def _write_raster(path, crs="EPSG:25832"):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    prof = dict(
        driver="GTiff",
        dtype="float32",
        count=1,
        width=40,
        height=30,
        crs=crs,
        nodata=-9999.0,
        transform=from_origin(500000, 5600030, 30, 30),
    )
    z = np.random.default_rng(0).random((30, 40)).astype("float32")
    z[0:4, 0:4] = -9999.0  # a nodata corner → must render transparent
    with rasterio.open(path, "w", **prof) as ds:
        ds.write(z, 1)
    return path


def test_render_map_renders_raster_as_image_overlay(tmp_path):
    from pathlib import Path

    tif = _write_raster(tmp_path / "tri.tif")
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    # a column passed for a raster must NOT error (it just doesn't apply)
    r = tools["render_map"](
        layers=[str(tif)],
        output_path="tri.html",
        column="TRI",
        cmap="RdYlGn",
    )
    assert r["ok"]
    html = Path(r["output"]).read_text()
    assert "imageoverlay" in html.lower()  # Leaflet raster overlay
    assert "data:image/png;base64" in html  # the reprojected image embedded


def test_render_map_stacks_raster_under_vector(tmp_path):
    sample = write_building_sample(tmp_path)
    tif = _write_raster(tmp_path / "dem.tif")
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](
        layers=[str(tif), str(sample["buildings"])],
        output_path="stack.html",
    )
    assert r["ok"] and r["layers"] == [str(tif), str(sample["buildings"])]


def test_render_map_tolerates_param_aliases(tmp_path):
    # LLMs reach for plural/singular variants; render_map accepts them instead of
    # crashing. The exact failing call from a real run used columns="[]".
    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))

    # columns="[]" (a JSON-array string meaning "no column") must not crash.
    r = tools["render_map"](
        layers=[str(sample["buildings"])],
        output_path="a.html",
        columns="[]",
    )
    assert r["ok"] and "choropleth" not in r

    # `layer` (singular) substitutes for `layers`.
    r2 = tools["render_map"](
        layer=str(sample["buildings"]),
        output_path="b.html",
    )
    assert r2["ok"] and r2["layers"] == [str(sample["buildings"])]

    # `columns` naming a real field drives the choropleth like `column` would.
    r3 = tools["render_map"](
        layers=[str(sample["buildings"])],
        output_path="c.html",
        columns="true_height",
    )
    assert r3["ok"] and r3["choropleth"]["column"] == "true_height"


def _inspect(tools, **kwargs):
    """Await `inspect_map` — it is a coroutine function, and must stay one.

    A synchronous tool is dispatched by pydantic-ai through `run_in_executor`, which
    forbids the nested `Agent.run_sync()` that the vision fallback needs. Tests call
    it the way the agent does: awaited, never as a plain function.
    """
    import asyncio

    return asyncio.run(tools["inspect_map"](**kwargs))


def test_inspect_map_always_registered(tmp_path):
    # Always available, whoever ends up looking: a text-only main model gets the
    # snapshot routed to `model.vision_model` instead of the image (see below).
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    assert "inspect_map" in tools


def test_inspect_map_returns_snapshot_image(tmp_path):
    from pydantic_ai import BinaryContent, ToolReturn

    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    out = _inspect(tools,layers=[str(sample["buildings"])], question="check")
    assert isinstance(out, ToolReturn)
    assert out.return_value["ok"] and out.return_value["layers"]
    images = [c for c in out.content if isinstance(c, BinaryContent)]
    assert images and images[0].media_type == "image/png" and images[0].data


def test_inspect_map_tolerates_render_map_style_aliases(tmp_path):
    from pydantic_ai import BinaryContent, ToolReturn

    # The model reaches for render_map's arg names (singular `layer`, a JSON-array
    # *string* `fields`, `column`) — these must reconcile, not crash. A repeated
    # mis-call previously raised UnexpectedModelBehavior and killed the whole run.
    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    out = _inspect(tools,
        layer=str(sample["buildings"]),
        fields='["name", "true_height"]',
        column="true_height",
        cmap="YlOrRd",
    )
    assert isinstance(out, ToolReturn)
    assert out.return_value["ok"] and out.return_value["layers"]
    images = [c for c in out.content if isinstance(c, BinaryContent)]
    assert images and images[0].media_type == "image/png"


def test_inspect_map_via_vision_model_without_config_notes(tmp_path):
    # via_vision_model with no configured fallback → a clear note, no crash.
    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path), vision_model=""))
    out = _inspect(tools,layers=[str(sample["buildings"])], via_vision_model=True)
    assert out["ok"] is False and "vision_model" in out["note"]


def _blind_main_model(monkeypatch, sees: bool | None = False):
    """Pretend the configured main model states it takes no image input."""
    from chester.capabilities import mapoutput

    monkeypatch.setattr(mapoutput, "sees_images", lambda *_a, **_k: sees)


def test_a_text_only_main_model_never_gets_the_image(tmp_path, monkeypatch):
    """The 2026-08-19 abort: attaching a PNG to a text-only model is fatal.

    Ollama rejects the whole request with HTTP 400 before the model can act on the
    "call again with via_vision_model=True" hint, the stream dies, and SelmaKit
    persists nothing — 634 s of correct geoprocessing, unreadable. So the snapshot
    goes to the fallback vision model without asking the model to notice first.
    """
    from pydantic_ai import ToolReturn

    from chester.capabilities import mapoutput

    sample = write_building_sample(tmp_path)
    _blind_main_model(monkeypatch)
    monkeypatch.setattr(mapoutput, "_ask_vision_model", lambda *_a, **_k: "sieht plausibel aus")
    tools = tools_of(
        MapOutputCapability(
            workspace=str(tmp_path),
            vision_model="ollama/qwen3-vl:latest",
            main_model="ollama/gemma4:26b-mlx",
        )
    )
    out = _inspect(tools,layers=[str(sample["buildings"])])
    assert not isinstance(out, ToolReturn)  # i.e. no BinaryContent went out
    assert out["ok"] and out["review"] == "sieht plausibel aus"
    # And it says who looked, so the verdict is not mistaken for the caller's own.
    assert "ollama/qwen3-vl:latest" in out["note"]


def test_the_vision_turn_runs_off_the_event_loop(tmp_path, monkeypatch):
    """Regression: the visual check must actually be able to run.

    Found in the `dop-ndvi-no-nir-bayern` benchmark run — every `inspect_map` call
    that reached the vision model came back
    ``"vision model 'ollama/qwen3-vl:latest' failed: UserError: Agent.run_sync() …
    cannot be used inside a synchronous tool"``. `inspect_map` was a plain `def`, so
    pydantic-ai dispatched it through `run_in_executor`, which flags the context and
    makes the nested run fail fast. The tool that lets the agent look at its own map
    had never once worked.

    Two conditions keep it working, and neither is visible in a return value:
    `inspect_map` stays a coroutine function (no flag gets set), and the blocking
    vision turn goes off the loop thread (a `run_sync` on the loop would raise too).
    """
    import asyncio
    import inspect as inspect_mod

    from chester.capabilities import mapoutput

    tools = tools_of(
        MapOutputCapability(
            workspace=str(tmp_path),
            vision_model="ollama/qwen3-vl:latest",
            main_model="ollama/gemma4:26b-mlx",
        )
    )
    assert inspect_mod.iscoroutinefunction(tools["inspect_map"])

    def _demands_its_own_thread(*_a, **_k):
        # `Agent.run_sync()` does this internally: it refuses to drive a loop that is
        # already running. Reached on the loop thread, the real call would raise.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return "OK"
        raise AssertionError("vision turn ran on the event loop — restore the to_thread hop")

    sample = write_building_sample(tmp_path)
    _blind_main_model(monkeypatch)
    monkeypatch.setattr(mapoutput, "_ask_vision_model", _demands_its_own_thread)
    out = _inspect(tools, layers=[str(sample["buildings"])])
    assert out["ok"] and out["review"] == "OK"


def test_a_blind_model_without_a_fallback_reports_that_nobody_looked(tmp_path, monkeypatch):
    """Inert, not fatal — and honest about which check did not happen."""
    from pydantic_ai import ToolReturn

    sample = write_building_sample(tmp_path)
    _blind_main_model(monkeypatch)
    tools = tools_of(
        MapOutputCapability(
            workspace=str(tmp_path), vision_model="", main_model="ollama/gemma4:26b-mlx"
        )
    )
    out = _inspect(tools,layers=[str(sample["buildings"])])
    assert not isinstance(out, ToolReturn)
    assert out["ok"] and out["layers"]  # the per-layer facts still stand
    assert "no image input" in out["note"]


def test_an_unknown_model_still_gets_the_image(tmp_path, monkeypatch):
    """Unknown must mean "carry on", never "cannot see" — the probe may not guess."""
    from pydantic_ai import BinaryContent, ToolReturn

    sample = write_building_sample(tmp_path)
    _blind_main_model(monkeypatch, sees=None)
    tools = tools_of(
        MapOutputCapability(
            workspace=str(tmp_path),
            vision_model="ollama/qwen3-vl:latest",
            main_model="anthropic/claude-opus-4-8",
        )
    )
    out = _inspect(tools,layers=[str(sample["buildings"])])
    assert isinstance(out, ToolReturn)
    assert [c for c in out.content if isinstance(c, BinaryContent)]


def test_the_instructions_require_naming_source_and_licence():
    """Gemessen 2026-09-05, `count-bus-stops-in-district`: zweimal am selben Kriterium
    durchgefallen („Meldet eine plausible Anzahl … mit Quelle + Lizenz").

    Die Lizenz lag dreimal im Lauf vor — `geodata_search` (cc-by/4.0), `gtfs_feeds`
    und `fetch_gtfs_stops` (CC-BY 4.0, gtfs.de) — und wurde nicht in die Antwort
    übernommen. Der Grund war kein Modellfehler: Die einzige Stelle im gesamten
    Systemprompt, die eine Lizenznennung verlangte, stand im 3D-Gebäude-Abschnitt.
    Für Kataloge, WFS, GTFS, amtliche Grenzen, DOP, DGM1 kein Wort. Ein Lauf fiel
    also durch für etwas, das ihm nie gesagt wurde — seit dem 2026-08-23.
    """
    from chester.capabilities.discovery import DataDiscoveryCapability

    text = DataDiscoveryCapability(workspace=".").get_instructions()(None)
    assert "licence" in text
    assert "final answer" in text
    # Die Regel muss an das maschinell vorhandene Feld gebunden sein, sonst ist sie
    # eine Stilbitte statt einer prüfbaren Bedingung.
    assert "`licence` field" in text


def test_vector_info_describes_a_table_without_geometry(tmp_path):
    """Gemessen 2026-09-05 in der Probe `join-leading-zero-ags`.

    Der Agent wollte vor dem Join wissen, welche Spalten `einwohner.csv` hat — die
    naheliegendste Frage überhaupt — und bekam `AttributeError: 'DataFrame' object
    has no attribute 'crs'` zurück, einen durchgereichten Python-Fehler. Danach wich
    er auf handgeschriebenes pandas in `qgis_python` aus.

    Die Spaltentypen sind hier nicht Beiwerk, sondern die Diagnose: AGS als `str`
    in der CSV, als `int64` im GeoPackage — der Join trifft nichts, und das Ergebnis
    sieht vollständig aus.
    """
    import shutil

    from chester.capabilities.vector import VectorCapability

    cache = tmp_path / "geocache"
    cache.mkdir(parents=True)
    shutil.copy("samples/probe/einwohner.csv", cache / "einwohner.csv")
    shutil.copy("samples/probe/gemeinden.gpkg", cache / "gemeinden.gpkg")
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))

    csv = tools["vector_info"]("einwohner.csv")
    assert csv["ok"], csv.get("error")
    assert csv["kind"] == "table"
    assert csv["crs"] is None
    assert csv["columns"]["ags"] == "str"

    gpkg = tools["vector_info"]("gemeinden.gpkg")
    assert gpkg["kind"] == "vector"
    assert gpkg["columns"]["ags"] == "int64"
    # Genau diese Differenz ist die Falle — beide Seiten aus je einem Aufruf lesbar.
    assert csv["columns"]["ags"] != gpkg["columns"]["ags"]


def test_vector_info_says_a_layer_is_mixed_before_the_damage(tmp_path):
    """Die Liste der Typen stand schon da — sie sagte nur nicht, was sie bedeutet.

    Gemessen 2026-09-05 (`supermarket-accessibility-choropleth`): `osm_features` gab
    `geometry_types: ["LineString","MultiPolygon","Point","Polygon"]` zurück, der
    Agent las das und clippte trotzdem. Gehandelt hat er erst, als eine Warnung ihm
    die **Folge** nannte — und da waren die 138 Polygone schon weg. Die Folge gehört
    deshalb vor den Schaden, nicht nur danach in den `qgis_run`-Rückgabewert.
    """
    import geopandas as gpd
    from shapely.geometry import Point, box

    from chester.geofacts import vector_facts

    mixed = tmp_path / "mixed.gpkg"
    gpd.GeoDataFrame({"x": [1, 2]}, geometry=[Point(700000, 5400000),
                                              box(700100, 5400100, 700200, 5400200)],
                     crs="EPSG:25832").to_file(mixed)
    f = vector_facts(str(mixed), full=True)
    assert f["mixed_geometry"] is True
    assert "MIXED GEOMETRY" in f["note"] and "point, polygon" in f["note"]
    assert "native:centroids" in f["note"], "die Notiz muss den Ausweg nennen"
    assert "too small" in f["note"], "und die Falle am Ausweg"


def test_a_single_family_layer_stays_quiet(tmp_path):
    """Polygon und MultiPolygon sind **eine** Familie — kein Anlass für eine Notiz."""
    import geopandas as gpd
    from shapely.geometry import MultiPolygon, box

    from chester.geofacts import vector_facts

    single = tmp_path / "single.gpkg"
    gpd.GeoDataFrame(
        {"x": [1, 2]},
        geometry=[box(0, 0, 4, 4), MultiPolygon([box(5, 5, 6, 6), box(7, 7, 8, 8)])],
        crs="EPSG:25832",
    ).to_file(single)
    f = vector_facts(str(single), full=True)
    assert "mixed_geometry" not in f and "note" not in f


def _mixed_layer(path):
    """Vier Objekte, vier verschiedene Geometrietypen — inklusive der Einzel/Mehrteil-
    Paare, an denen sich zeigt, ob ein Split umformt."""
    import geopandas as gpd
    from shapely.geometry import MultiPoint, MultiPolygon, Point, box

    gpd.GeoDataFrame(
        {"id": [1, 2, 3, 4]},
        geometry=[
            Point(700000, 5400000),
            MultiPoint([(700010, 5400010), (700020, 5400020)]),
            box(700100, 5400100, 700200, 5400200),
            MultiPolygon([box(700300, 5400300, 700310, 5400310),
                          box(700320, 5400320, 700330, 5400330)]),
        ],
        crs="EPSG:25832",
    ).to_file(path)
    return str(path)


def _split_tool(tmp_path):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    return tools_of(VectorCapability(workspace=str(tmp_path)))["vector_split_by_geometry"]


def test_split_writes_one_file_per_geometry_type(tmp_path):
    """Eine Datei je Typ, jede mit einem Kopf, der zu ihrem Inhalt passt."""
    import os

    from chester.capabilities.qgis import _declared_geometry_type

    tool = _split_tool(tmp_path)
    _mixed_layer(tmp_path / "geocache" / "mixed.gpkg")
    res = tool(path="mixed.gpkg", output_prefix="parts")
    assert res["ok"] is True and res["features"] == 4
    got = {p["geometry_type"]: p for p in res["parts"]}
    assert set(got) == {"Point", "MultiPoint", "Polygon", "MultiPolygon"}
    for geom_type, part in got.items():
        assert part["features"] == 1
        assert _declared_geometry_type(part["output"]) == geom_type
        assert os.path.isfile(part["output"] + ".meta.json"), "Provenienz fehlt"


def test_split_changes_nothing_it_only_splits(tmp_path):
    """Die eigentliche Zusage: Punkte bleiben Punkte, Flächen bleiben Flächen.

    Nutzerkorrektur 2026-09-05. Eine erste Fassung gruppierte nach Geometrie-
    **Familie**; dabei muss der Schreiber innerhalb einer Gruppe auf einen Typ
    vereinheitlichen und befördert Einzel- zu Mehrteil — gemessen wurde aus einem
    `Point` ein `MultiPoint` und aus einem `Polygon` ein `MultiPolygon`. Ein Werkzeug,
    das aufteilen soll, darf nichts umformen; sonst ist es ein zweites `centroids`.
    Gruppiert wird deshalb nach dem **exakten** Typ.
    """
    import geopandas as gpd
    import pandas as pd

    tool = _split_tool(tmp_path)
    src = _mixed_layer(tmp_path / "geocache" / "mixed.gpkg")
    before = gpd.read_file(src)
    res = tool(path="mixed.gpkg", output_prefix="parts")

    after = gpd.GeoDataFrame(
        pd.concat([gpd.read_file(p["output"]) for p in res["parts"]]), crs=before.crs
    ).sort_values("id").reset_index(drop=True)
    assert list(before.geom_type) == list(after.geom_type), "ein Typ wurde umgeformt"
    assert all(a.equals_exact(b, 0) for a, b in zip(before.geometry, after.geometry))
    assert before.drop(columns="geometry").equals(after.drop(columns="geometry"))
    assert before.crs == after.crs
    assert "nothing was converted" in res["note"]


def test_split_refuses_a_layer_that_needs_no_split(tmp_path):
    """Ein Typ heißt: nichts zu tun, und das gehört gesagt statt getan."""
    import geopandas as gpd
    from shapely.geometry import box

    tool = _split_tool(tmp_path)
    gpd.GeoDataFrame({"x": [1]}, geometry=[box(0, 0, 4, 4)], crs="EPSG:25832").to_file(
        tmp_path / "geocache" / "single.gpkg")
    res = tool(path="single.gpkg", output_prefix="parts")
    assert res["ok"] is False
    assert "nothing to split" in res["error"] and res["geometry_types"] == ["Polygon"]


def test_the_mixed_geometry_note_lives_where_the_layer_is_born(tmp_path):
    """Der Hinweis muss dort stehen, wo der Agent hinsieht.

    Gemessen 2026-09-05 (`supermarket-accessibility-choropleth`, 1495 s): Der Lauf
    rief `vector_info` **kein einziges Mal** auf — die dort gebaute Notiz erreichte
    ihn nie. Von der Mischung wusste er trotzdem, aus `geometry_types` in der
    `osm_features`-Rueckgabe; genau dort entsteht die Ebene, und dort gehoert die
    Folge hin. Ein Text, eine Funktion, drei Aufrufstellen.
    """
    from chester.geofacts import mixed_geometry_note

    note = mixed_geometry_note(["Point", "Polygon"])
    assert note is not None
    assert "MIXED GEOMETRY" in note and "point, polygon" in note
    assert "native:centroids" in note, "der Weg zum Zaehlen"
    assert "vector_split_by_geometry" in note, "der verlustfreie Weg zum Messen"
    assert "too small" in note, "und was der Schwerpunkt kostet"


def test_a_single_family_layer_gets_no_note():
    """Polygon und MultiPolygon sind eine Familie — kein Anlass fuer Laerm."""
    from chester.geofacts import mixed_geometry_note

    assert mixed_geometry_note(["Polygon", "MultiPolygon"]) is None
    assert mixed_geometry_note(["Point"]) is None
    assert mixed_geometry_note([]) is None
    assert mixed_geometry_note(None) is None


def test_osm_features_carries_the_note_in_its_return():
    """Die Rueckgabe von `osm_features` traegt sie, nicht nur `vector_info`."""
    import inspect

    from chester.capabilities import discovery

    src = inspect.getsource(discovery)
    assert src.count("mixed_geometry_note(geom_types)") == 3, (
        "alle drei Werkzeuge, die eine Vektorebene herunterladen, muessen sie tragen"
    )
