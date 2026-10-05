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
    """An SQL expression must be reported as an SQL expression, not as a quoting question.

    Occasion (2026-09-03, `laguna-xs-2.1` on `pluvial-flow-accumulation-tegernheim`): the
    model wrote `"waterway" IN ('stream', …) AND geometry IS NOT NULL`, got a SyntaxError
    with a hint about quotes and backticks, followed the hint, failed again — and then
    fell back on hand-written PyQGIS. The old hint was not wrong, it just did not fit the
    error, and that costs more than none.
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
    assert "and" in r["hint"] and "AND" in r["hint"]  # the concrete spot, not the rule
    # The two tools that really accept SQL-like expressions.
    assert "vector_extract_by_attribute" in r["hint"]
    assert "native:extractbyexpression" in r["hint"]


def test_vector_filter_keeps_the_quoting_hint_where_it_fits(tmp_path):
    """Without SQL features the old hint stays — it was right for its case."""
    sample = write_building_sample(tmp_path)
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    r = tools["vector_filter"](
        path=str(sample["buildings"]), expression="true_height >>> 3", output_path="x.geojson"
    )

    assert r["ok"] is False
    assert "backticks" in r["hint"] and "pandas" not in r["hint"]


def test_vector_filter_lists_columns_only_when_one_might_be_missing(tmp_path):
    """For a syntax error the column list does not help — it only fills the context.

    In the triggering case 40 OSM attribute names like `TMC:cid_58:tabcd_1:LocationCode`
    came back, while the error was a syntax error.
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

    from chester.discoveryshared import _apply_where

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
    linked a map and pre-excused its absence ("due to the file size").
    """
    from chester import mapguards

    sample = write_building_sample(tmp_path)
    monkeypatch.setattr(mapguards, "MAX_INLINE_MB", 1e-9)  # force the size backstop
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](layers=[str(sample["buildings"])], output_path="big.html")

    assert r["ok"] is False and r["embedded"] is False
    assert "output" not in r and "picture" not in r  # nothing to quote
    assert "NO file was written" in r["reason"]
    assert r["recommend_tool"] == "qgis_show"
    assert not (tmp_path / "geocache" / "big.html").exists()


def test_render_map_feature_guard_reports_failure_not_success(tmp_path, monkeypatch):
    """The cheap pre-check ahead of the render has the same contract."""
    from chester import mapguards

    sample = write_building_sample(tmp_path)
    monkeypatch.setattr(mapguards, "MAX_INLINE_FEATURES", 0)  # force the pre-check
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](layers=[str(sample["buildings"])], output_path="big.html")

    assert r["ok"] is False and r["embedded"] is False
    assert "output" not in r
    assert "NO file was written" in r["reason"]


def test_render_map_vertex_guard_falls_back_to_the_picture(tmp_path, monkeypatch):
    """Too many vertices → the image is the result, not nothing.

    Occasion: 6,888 contour lines slipped through **both** old guards — far below the
    50,000-feature limit, at 42.5 MB just under the 45 MB cap — and the HTML stayed white
    in the browser (2026-09-02, `pluvial-flow-accumulation-tegernheim`). Measured, there
    were 936,687 vertices; they decide the rendering load, not the feature count and not
    the bytes.

    Unlike the other two guards the result here is **not** a failure: the same map
    image exists as a PNG, and a PNG is exactly what a reader needs for too many lines.
    Reporting it as `ok: false` would hide an existing result.
    """
    from pathlib import Path

    from chester import mapguards

    sample = write_building_sample(tmp_path)
    monkeypatch.setattr(mapguards, "MAX_INLINE_VERTICES", 0)  # Guard erzwingen
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](layers=[str(sample["buildings"])], output_path="dense.html")

    assert r["ok"] is True and r["embedded"] is False
    assert r["output"] == r["picture"], "das Bild IST die Ausgabe, kein Anhang"
    assert r["output"].endswith(".png")
    assert Path(r["output"]).is_file()
    assert not (tmp_path / "geocache" / "dense.html").exists(), "die HTML muss weg sein"
    assert r["vertices"] > 0
    # The way out must be in the return value, not only in the instruction — in this
    # project that is the channel that turns behaviour.
    assert "STATIC PICTURE" in r["reason"] and "report its path" in r["reason"]


def test_render_map_vertex_guard_counts_real_geometry(tmp_path):
    """The counter must count vertices, not features — otherwise it measures the wrong thing."""
    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](layers=[str(sample["buildings"])], output_path="ok.html")

    assert r["ok"] is True
    # Below the limit the guard does not trip: no `vertices`, no `reason`.
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


def test_render_map_wms_alone_carries_the_map(tmp_path):
    """Without a local layer the WMS founds the map itself — and must still write one.

    Nothing sets the extent in this case, so the code asks the service for its
    advertised bbox. An unreachable service must not cost the caller their map: it
    falls back to a Germany-wide view. The address points at a closed local port,
    so the failure is immediate and the test needs no network.

    Written when this path moved into `chester/maprender.py` (phase KM step 1.5) and
    turned out to be the one branch of `render_map` no test covered.
    """
    from pathlib import Path

    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](
        output_path="nur_wms.html",
        wms_url="http://127.0.0.1:1/wms",
        wms_layer="test:schicht",
        wms_attribution="© Testdienst",
    )
    assert r["ok"] and r["layers"] == []
    html = Path(r["output"]).read_text()
    assert "test:schicht" in html and "© Testdienst" in html
    import re

    # The DE fallback view. Matched by pattern, not spelling: how many decimals
    # folium writes depends on whether `cjio` has been imported in this process —
    # it replaces the stdlib JSON float encoder globally (see `chester/citymodel.py`).
    assert '"zoom": 6' in html
    assert re.search(r"\[51\.0+, 10\.0+\]", html)


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
    from chester.runtime import mapinspect as mapoutput  # inspect_map lives in runtime

    monkeypatch.setattr(mapoutput, "sees_images", lambda *_a, **_k: sees)


def test_a_text_only_main_model_never_gets_the_image(tmp_path, monkeypatch):
    """The 2026-08-19 abort: attaching a PNG to a text-only model is fatal.

    Ollama rejects the whole request with HTTP 400 before the model can act on the
    "call again with via_vision_model=True" hint, the stream dies, and SelmaKit
    persists nothing — 634 s of correct geoprocessing, unreadable. So the snapshot
    goes to the fallback vision model without asking the model to notice first.
    """
    from pydantic_ai import ToolReturn

    from chester.runtime import mapinspect as mapoutput  # inspect_map lives in runtime

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

    from chester.runtime import mapinspect as mapoutput  # inspect_map lives in runtime

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
    """Measured 2026-09-05, `count-bus-stops-in-district`: failed twice on the same
    criterion ("Meldet eine plausible Anzahl … mit Quelle + Lizenz").

    The licence was present three times in the run — `geodata_search` (cc-by/4.0),
    `gtfs_feeds` and `fetch_gtfs_stops` (CC-BY 4.0, gtfs.de) — and was not carried into
    the answer. The cause was no model error: the only place in the whole system prompt
    that asked for a licence was in the 3D-building section. For catalogues, WFS, GTFS,
    official boundaries, DOP, DGM1 not a word. So a run failed for something it was never
    told — since 2026-08-23.
    """
    from chester.capabilities.discovery import DataDiscoveryCapability

    text = DataDiscoveryCapability(workspace=".").get_instructions()(None)
    assert "licence" in text
    assert "final answer" in text
    # The rule must be bound to the field that is there by machine, otherwise it is a
    # style request instead of a checkable condition.
    assert "`licence` field" in text


def test_vector_info_describes_a_table_without_geometry(tmp_path):
    """Measured 2026-09-05 in the probe `join-leading-zero-ags`.

    Before the join the agent wanted to know which columns `einwohner.csv` has — the most
    obvious question there is — and got back `AttributeError: 'DataFrame' object has no
    attribute 'crs'`, a passed-through Python error. Then it fell back on hand-written
    pandas in `qgis_python`.

    The column types are not an extra here but the diagnosis: AGS as `str` in the CSV,
    as `int64` in the GeoPackage — the join hits nothing, and the result looks complete.
    """
    import shutil

    from chester.capabilities.vector import VectorCapability

    cache = tmp_path / "geocache"
    cache.mkdir(parents=True)
    shutil.copy("probes/fixtures/einwohner.csv", cache / "einwohner.csv")
    shutil.copy("probes/fixtures/gemeinden.gpkg", cache / "gemeinden.gpkg")
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))

    csv = tools["vector_info"]("einwohner.csv")
    assert csv["ok"], csv.get("error")
    assert csv["kind"] == "table"
    assert csv["crs"] is None
    assert csv["columns"]["ags"] == "str"

    gpkg = tools["vector_info"]("gemeinden.gpkg")
    assert gpkg["kind"] == "vector"
    assert gpkg["columns"]["ags"] == "int64"
    # Exactly this difference is the trap — both sides readable from one call each.
    assert csv["columns"]["ags"] != gpkg["columns"]["ags"]


def test_vector_info_says_a_layer_is_mixed_before_the_damage(tmp_path):
    """The list of types was there already — it just did not say what it means.

    Measured 2026-09-05 (`supermarket-accessibility-choropleth`): `osm_features` returned
    `geometry_types: ["LineString","MultiPolygon","Point","Polygon"]`, the agent read it
    and clipped anyway. It acted only once a warning named the **consequence** — and by
    then the 138 polygons were gone. So the consequence belongs before the damage, not
    only afterwards in the `qgis_run` return value.
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
    """Polygon and MultiPolygon are **one** family — no reason for a note."""
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
    """Four features, four different geometry types — including the single/multi-part
    pairs that show whether a split converts."""
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
    """One file per type, each with a header that fits its content."""
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
    """The actual promise: points stay points, polygons stay polygons.

    User correction 2026-09-05. A first version grouped by geometry **family**; then the
    writer must unify one type per group and promotes single to multi-part — measured, a
    `Point` became a `MultiPoint` and a `Polygon` a `MultiPolygon`. A tool that is to
    split must not convert anything; otherwise it is a second `centroids`. So grouping is
    by the **exact** type.
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
    """One type means: nothing to do, and that should be said rather than done."""
    import geopandas as gpd
    from shapely.geometry import box

    tool = _split_tool(tmp_path)
    gpd.GeoDataFrame({"x": [1]}, geometry=[box(0, 0, 4, 4)], crs="EPSG:25832").to_file(
        tmp_path / "geocache" / "single.gpkg")
    res = tool(path="single.gpkg", output_prefix="parts")
    assert res["ok"] is False
    assert "nothing to split" in res["error"] and res["geometry_types"] == ["Polygon"]


def test_the_mixed_geometry_note_lives_where_the_layer_is_born(tmp_path):
    """The hint must stand where the agent looks.

    Measured 2026-09-05 (`supermarket-accessibility-choropleth`, 1495 s): the run did not
    call `vector_info` **a single time** — the note built there never reached it. It knew
    about the mix anyway, from `geometry_types` in the `osm_features` return; that is
    exactly where the layer is born, and that is where the consequence belongs. One text,
    one function, three call sites.
    """
    from chester.geofacts import mixed_geometry_note

    note = mixed_geometry_note(["Point", "Polygon"])
    assert note is not None
    assert "MIXED GEOMETRY" in note and "point, polygon" in note
    assert "native:centroids" in note, "der Weg zum Zaehlen"
    assert "vector_split_by_geometry" in note, "der verlustfreie Weg zum Messen"
    assert "too small" in note, "und was der Schwerpunkt kostet"


def test_a_single_family_layer_gets_no_note():
    """Polygon and MultiPolygon are one family — no reason for noise."""
    from chester.geofacts import mixed_geometry_note

    assert mixed_geometry_note(["Polygon", "MultiPolygon"]) is None
    assert mixed_geometry_note(["Point"]) is None
    assert mixed_geometry_note([]) is None
    assert mixed_geometry_note(None) is None


def test_every_vector_download_carries_the_note_in_its_return():
    """The return carries it, not only `vector_info`.

    Since Phase KM step 1 the three tools live in three wrapper modules — so the law
    sums over their sources instead of over `discovery`. If another tool that downloads
    a vector layer moves here, it must bring the note along and this number must rise.
    """
    import inspect

    from chester import filetools, ogctools, osmtools

    src = "".join(inspect.getsource(m) for m in (osmtools, ogctools, filetools))
    assert src.count("mixed_geometry_note(geom_types)") == 3, (
        "alle drei Werkzeuge, die eine Vektorebene herunterladen, muessen sie tragen"
    )


def test_the_overflow_store_is_wired_and_the_web_tools_are_not_duplicated():
    """`read_tool_result` comes from Chester, the web tools from SelmaKit.

    Corrected on 2026-09-06. A measurement had found "85 tools, none with web access" and
    looked only at `geo_capabilities()`; `selmakit.default_capabilities` has long shipped
    `WebSearch(local="duckduckgo")` and `local_web_fetch()`. Wiring them a second time here
    made every run die after 0.1 s on the name clash. So Chester contributes only two
    things: the overflow store (`ToolOutputLimits` → `read_tool_result`) and the
    instruction that draws the line — documentation yes, geodata no.
    """
    from agent_build import geo_capabilities

    names = []
    for cap in geo_capabilities():
        acc = {}

        def collect(t):
            if t is None:
                return
            tools = getattr(t, "tools", None)
            if isinstance(tools, dict):
                acc.update(tools)
            for attr in ("toolsets", "_toolsets", "toolset", "_toolset", "wrapped"):
                sub = getattr(t, attr, None)
                if sub is None:
                    continue
                for s in sub if isinstance(sub, (list, tuple)) else [sub]:
                    collect(s)

        collect(cap.get_toolset())
        names += list(acc)

    assert "read_tool_result" in names, "der Ueberlaufspeicher fehlt"
    for from_selmakit in ("duckduckgo_search", "web_fetch"):
        assert from_selmakit not in names, (
            f"{from_selmakit} kommt von SelmaKit — hier waere es ein Namenskonflikt")
    assert "delegate_task" not in names, (
        "Sub-Agenten sind aus — das Werkzeug haette keinen Adressaten")
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"doppelt verdrahtet: {dupes}"


def _geo_run(tmp_path):
    """`geo_python_run` on a fresh workspace, as a **direct call**.

    A context without a conversation: then `_checked_route_guard` stands aside, because
    there is no round to count — the same rule as for the PyQGIS guard. The guard itself
    is tested in its own tests, not here in passing.
    """
    from functools import partial
    from types import SimpleNamespace

    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    tool = tools_of(VectorCapability(workspace=str(tmp_path)))["geo_python_run"]
    return partial(tool, SimpleNamespace(messages=[], run_id=None))


def test_geo_python_run_binds_the_stack_and_returns_a_result(tmp_path):
    """The escape hatch without QGIS: geopandas/shapely are in the namespace.

    Phase KQ step 1. The same mechanism as `qgis_python` — subprocess, curated namespace,
    JSON verdict, time limit — only it points at Chester's own interpreter instead of
    QGIS's. The subprocess stays anyway: the time limit is enforceable, a GDAL segfault
    kills the child instead of the agent, and Chester's process state stays untouched.
    """
    res = _geo_run(tmp_path)(code=(
        "g = gpd.GeoDataFrame({'x': [1, 2]}, geometry=[Point(0, 0), box(0, 0, 2, 2)],\n"
        "                     crs='EPSG:25832')\n"
        "print('hallo')\n"
        "result = {'n': len(g), 'types': sorted(set(g.geom_type))}"
    ))
    assert res["ok"] is True, res.get("error")
    assert res["result"] == {"n": 2, "types": ["Point", "Polygon"]}
    assert "hallo" in res["stdout"]


def test_geo_python_run_reports_its_calls_in_the_content(tmp_path):
    """`calls` stands in the **content** of the return, not beside it.

    The lesson from the CodeMode finding (2026-09-06): `selmakit.tool_returns` reads
    `part.content` and discards `part.metadata`. What happens in a subprocess would be
    invisible as metadata — and the gate would fail silently. So the checked helpers
    write their calls into the content.
    """
    import os

    import geopandas as gpd
    from shapely.geometry import Point, box

    tool = _geo_run(tmp_path)
    gpd.GeoDataFrame({"x": [1, 2]}, geometry=[Point(700000, 5400000),
                                              box(700100, 5400100, 700200, 5400200)],
                     crs="EPSG:25832").to_file(tmp_path / "geocache" / "mixed.gpkg")
    res = tool(code=(
        "g = read_vector('mixed.gpkg')\n"
        "result = write_vector(g[g.geom_type == 'Point'], 'only_points.gpkg')"
    ))
    assert res["ok"] is True, res.get("error")
    names = [c["name"] for c in res["calls"]]
    assert names == ["read_vector", "write_vector"]
    # the reading helper reports the mixed layer before anything is computed on it
    assert "MIXED GEOMETRY" in res["calls"][0]["warning"]
    # and the output is recorded rather than guessed — with provenance
    assert res["outputs"] and os.path.isfile(res["outputs"][0] + ".meta.json")


def test_geo_python_run_reports_a_failure_instead_of_crashing(tmp_path):
    """An error in the snippet is a verdict, not a crash of the agent."""
    res = _geo_run(tmp_path)(code="result = 1 / 0")
    assert res["ok"] is False
    assert "ZeroDivisionError" in res["error"]


def test_the_nine_operations_sit_next_to_the_raw_stack_in_the_sandbox(tmp_path):
    """The difference to CodeMode: a checked function AND raw geopandas, one snippet.

    Phase KQ step 2. In CodeMode only what is foreseen works — that closes the escape
    hatch `height-gini` needs (there is no algorithm for a Gini coefficient). Here the
    model calls `clip(...)` when it fits and computes by hand when not.
    """
    import geopandas as gpd
    from shapely.geometry import Point, box

    tool = _geo_run(tmp_path)
    gpd.GeoDataFrame({"x": [1, 2, 3]},
                     geometry=[Point(1, 1), box(0, 0, 4, 4), Point(500, 500)],
                     crs="EPSG:25832").to_file(tmp_path / "geocache" / "mixed.gpkg")
    gpd.GeoDataFrame({"x": [1]}, geometry=[box(-1, -1, 10, 10)],
                     crs="EPSG:25832").to_file(tmp_path / "geocache" / "mask.gpkg")
    res = tool(code=(
        "c = clip('mixed.gpkg', 'mask.gpkg', 'cut.gpkg')\n"
        "g = read_vector('cut.gpkg')\n"
        "eigene = float(g[g.geom_type == 'Polygon'].geometry.area.sum())\n"
        "result = {'out': c['features_out'], 'flaeche': eigene}"
    ))
    assert res["ok"] is True, res.get("error")
    assert res["result"]["out"] == 2, "beide Geometriearten ueberleben den Clip"
    assert res["result"]["flaeche"] == 16.0, "die Handrechnung lief im selben Schnipsel"
    assert [c["name"] for c in res["calls"]] == ["clip", "read_vector"]
    assert res["outputs"], "die Operation traegt ihre Ausgabe in outputs ein"


def test_a_checked_operation_inside_the_sandbox_resolves_paths_correctly(tmp_path):
    """Two path contracts that collide — measured, not assumed.

    `chester.workspace.resolve_path` counts from the repo root, the snippet runs in the
    GeoCache. Without the explicitly passed workspace, `reproject(…, 'x.gpkg')` wrote to
    `<geocache>/.chester/workspace/geocache/x.gpkg` — the same doubled path that has
    caught this project twice already.
    """
    import geopandas as gpd
    from shapely.geometry import box

    tool = _geo_run(tmp_path)
    gpd.GeoDataFrame({"x": [1]}, geometry=[box(0, 0, 4, 4)],
                     crs="EPSG:25832").to_file(tmp_path / "geocache" / "src.gpkg")
    res = tool(code="result = reproject('src.gpkg', 'wgs.gpkg', 'EPSG:4326')['output']")
    assert res["ok"] is True, res.get("error")
    assert (tmp_path / "geocache" / "wgs.gpkg").is_file()
    assert not (tmp_path / "geocache" / ".chester").exists(), "doppelter Pfad"


def test_without_qgis_chester_is_still_complete(monkeypatch):
    """QGIS is an option, not a prerequisite (Phase KQ step 4).

    Measured 2026-09-06: without QGIS the agent does build, but `qgis_search` raised
    `QgisNotFoundError`, and nineteen unusable tools stood in the prompt. A tool that
    cannot run is prompt cost, not a feature — so the three QGIS capabilities stay out
    entirely. Filtering happens at the **capability** level so the instruction sections
    go along.
    """
    import agent_build

    def collect_tools(caps):
        found = set()
        for cap in caps:
            acc = {}

            def walk(t):
                if t is None:
                    return
                tools = getattr(t, "tools", None)
                if isinstance(tools, dict):
                    acc.update(tools)
                for attr in ("toolsets", "_toolsets", "toolset", "_toolset", "wrapped"):
                    sub = getattr(t, attr, None)
                    if sub is None:
                        continue
                    for s in sub if isinstance(sub, (list, tuple)) else [sub]:
                        walk(s)

            walk(cap.get_toolset())
            found |= set(acc)
        return found

    monkeypatch.setattr(agent_build, "qgis_available", lambda: False)
    caps = agent_build.geo_capabilities()
    names = {type(c).__name__ for c in caps}
    tools = collect_tools(caps)

    assert not (names & {"QgisToolboxCapability", "GeoPyCapability", "GeoLiveCapability"})
    assert not [t for t in tools if t.startswith("qgis_")], "totes Werkzeug im Prompt"
    # the computing core stays reachable — that is why it sits on the VectorCapability
    assert "geo_python_run" in tools
    for essential in ("vector_info", "osm_features", "render_map", "geocode"):
        assert essential in tools, f"{essential} fehlt ohne QGIS"


def test_the_config_key_is_read(tmp_path):
    """`geodata.use_qgis: false` is read from `chester.json`."""
    import json

    from chester.geoconfig import load_geodata

    (tmp_path / "chester.json").write_text(
        json.dumps({"geodata": {"use_qgis": False}}), encoding="utf-8")
    assert load_geodata(state_dir=str(tmp_path))["use_qgis"] is False


def test_qgis_is_switched_off_when_the_config_says_so(monkeypatch):
    """The switch without which QGIS-less mode is untestable.

    Measured 2026-09-06: pointing `CHESTER_QGIS_PROCESS_BIN`/`CHESTER_QGIS_APP` at nothing
    is **not** enough — the candidate search then falls back to `/Applications` and finds
    an installed QGIS anyway. Without this switch the mode most users will run in could
    only be tested by monkeypatch; and both branches must be measurable on one machine.
    """
    from chester import geoconfig, qgis_env

    monkeypatch.delenv("CHESTER_NO_QGIS", raising=False)
    monkeypatch.setattr(geoconfig, "load_geodata", lambda *a, **k: {"use_qgis": False})
    assert qgis_env.qgis_disabled() is True
    assert qgis_env.qgis_available() is False


def test_the_env_variable_overrides_the_config_both_ways(monkeypatch):
    """For a single run, without rewriting the config — so both branches can be measured
    against each other on the same machine."""
    from chester import qgis_env

    monkeypatch.setenv("CHESTER_NO_QGIS", "1")
    assert qgis_env.qgis_disabled() is True
    monkeypatch.setenv("CHESTER_NO_QGIS", "0")
    assert qgis_env.qgis_disabled() is False, "=0 muss den Schalter aufheben"


def test_the_default_keeps_qgis_on():
    """A missing or unreadable config must switch nothing off."""
    from chester.geoconfig import load_geodata

    assert load_geodata(state_dir="/nonexistent")["use_qgis"] is True


def test_the_full_agent_has_no_duplicate_tool_names():
    """The test whose absence let a run die after 0.1 s.

    Measured 2026-09-06: I had wired `WebSearch`/`WebFetch` into `geo_capabilities()`,
    because a measurement found "85 tools, none with web access". The measurement looked
    only at `geo_capabilities()` — but `selmakit.default_capabilities` has long brought
    `WebSearch(local="duckduckgo")` and `local_web_fetch()`. Result:

        UserError: FunctionToolset defines a tool whose name conflicts with existing
        tool from FunctionToolset: 'duckduckgo_search'

    Every run aborted before the model saw a word, and `./check.sh` stayed green,
    because no test built the **combined** set. That is exactly what this one does.
    """
    from selmakit import Gateway

    import agent_build

    # If the gateway builds, no tool name is doubled — pydantic-ai checks that when
    # merging the toolsets and raises `UserError` otherwise.
    gateway = Gateway.from_config(
        capabilities=agent_build.selmakit_capabilities,
        extra_capabilities=agent_build.geo_capabilities(),
    )
    assert gateway is not None


def _guard_ctx(parts):
    """A RunContext whose run already has these tool returns."""
    from types import SimpleNamespace

    from pydantic_ai.messages import ModelRequest

    req = ModelRequest(parts=parts)
    try:
        req.run_id = "R1"
        run_id = "R1"
    except Exception:  # noqa: BLE001 - older message models have no run_id
        run_id = None
    return SimpleNamespace(messages=[req], run_id=run_id)


def _guard_return(ok):
    from pydantic_ai.messages import ToolReturnPart

    from chester.runtime.geopython import _GUARD_MARKER

    content = ({"ok": True, "result": "x", "outputs": [], "calls": []} if ok
               else {"ok": False, "error": f"{_GUARD_MARKER}: …"})
    return ToolReturnPart(tool_name="geo_python_run", content=content, tool_call_id="c")


_RAW_SNIPPET = (
    "import geopandas as gpd\n"
    "gdf = gpd.read_file('x.gpkg')\n"
    "gdf = gdf.to_crs(epsg=25832)\n"
    "gdf['geometry'] = gdf.geometry.buffer(500)\n"
    "gdf.to_file('out.gpkg')\n"
)


def test_the_guard_names_the_checked_function_a_snippet_rebuilds():
    """The escape hatch must not become the main road.

    Measured 2026-09-06 (`buffer-schools-500m`, QGIS switched off): the agent found
    `geo_python_run` at once and wrote raw geopandas in it three times. Correct on the
    merits — 84 buffers, 784,137 m² against 785,398 m² expected — and every guarantee ran
    into nothing: `outputs: []`, `calls: []`, **not a single provenance sidecar**.
    """
    from pydantic_ai.messages import ToolReturnPart

    from chester.runtime.geopython import _checked_route_guard

    ctx = _guard_ctx([ToolReturnPart(tool_name="geocode", content={}, tool_call_id="c0")])
    res = _checked_route_guard(ctx, _RAW_SNIPPET)
    assert res is not None and res["ok"] is False
    # The **tool names** are named — that is the lesson from 2026-09-07: the agent used
    # `vector_split_by_geometry` unprompted (it is in the catalogue), but never touched the
    # same operations in the namespace and wrote "Since I can't call 'reproject' inside
    # here". What is in the catalogue gets used.
    assert set(res["checked_functions"]) == {"vector_reproject", "vector_buffer",
                                             "read_vector", "write_vector"}
    assert "These are **tools**" in res["error"], "die Abweisung muss auf Werkzeuge zeigen"
    assert "provenance" in res["error"], "und den Preis der Handarbeit benennen"


def test_the_guard_lets_the_same_snippet_through_on_the_second_try():
    """One round only — the lesson from the history of the PyQGIS guard.

    There are tasks without a checked operation (a Gini coefficient, a kernel density). A
    gate that pushes even then turns a fixable circumstance into a dead end; on
    2026-09-01 exactly that cost three rounds.
    """
    from chester.runtime.geopython import _checked_route_guard

    ctx = _guard_ctx([_guard_return(ok=False)])
    assert _checked_route_guard(ctx, _RAW_SNIPPET) is None


def test_the_guard_re_arms_after_a_snippet_has_run():
    """One refusal must not open the whole run.

    That is exactly what happened to the PyQGIS guard on 2026-08-27: one search, then
    twelve hand-written blocks.
    """
    from chester.runtime.geopython import _checked_route_guard

    ctx = _guard_ctx([_guard_return(ok=False), _guard_return(ok=True)])
    assert _checked_route_guard(ctx, _RAW_SNIPPET) is not None


def test_a_snippet_that_uses_the_checked_functions_is_never_stopped():
    """Whoever calls `clip(...)` is not stopped — not even beside raw geopandas."""
    from pydantic_ai.messages import ToolReturnPart

    from chester.runtime.geopython import _checked_route_guard

    ctx = _guard_ctx([ToolReturnPart(tool_name="geocode", content={}, tool_call_id="c0")])
    good = ("g = read_vector('x.gpkg')\n"
            "r = reproject('x.gpkg', 'm.gpkg', 'EPSG:25832')\n"
            "b = buffer('m.gpkg', 'b.gpkg', 500)\n"
            "eigene = float(g.geometry.area.sum())\n"
            "write_vector(g, 'out.gpkg')\n")
    assert _checked_route_guard(ctx, good) is None


def test_the_guard_is_bounded():
    """After `_GUARD_MAX` refusals in a run it falls silent — no model can be pushed
    endlessly, and a loop costs more than a missing warning."""
    from chester.runtime.geopython import _GUARD_MAX, _checked_route_guard

    parts = []
    for _ in range(_GUARD_MAX):
        parts += [_guard_return(ok=False), _guard_return(ok=True)]
    assert _checked_route_guard(_guard_ctx(parts), _RAW_SNIPPET) is None


def test_the_nine_operations_are_tools_not_only_namespace_entries(tmp_path):
    """What is in the tool catalogue gets used; what is only in the prose does not.

    Measured 2026-09-07 (`buffer-schools-500m`, QGIS off): the agent called
    `vector_split_by_geometry` on its own — it is a tool — and used the same operations
    in the sandbox namespace **not a single time**, with the reason in its own snippets:
    "Since I can't call 'reproject' inside here", "Attempting to see if the tool
    'reproject' is available in the scope". Ten `geo_python_run` calls, `outputs: []` and
    `calls: []` throughout, no provenance sidecar. The instruction claimed they were
    bound; the model did not believe it.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    for name in ("vector_reproject", "vector_buffer", "vector_clip",
                 "vector_intersection", "vector_extract_by_location",
                 "vector_extract_by_attribute", "vector_dissolve",
                 "vector_add_field", "vector_field_sum"):
        assert name in tools, f"{name} fehlt im Werkzeugkatalog"

    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame({"x": [1]}, geometry=[Point(12.09, 49.01)],
                     crs="EPSG:4326").to_file(tmp_path / "geocache" / "p.gpkg")
    # The trap travels with the tool, not only with the function
    res = tools["vector_buffer"](input_path="p.gpkg", output_path="b.gpkg", distance=500)
    assert res["ok"] is False and "DEGREES" in res["error"]


def test_a_tool_call_stamps_provenance_where_a_snippet_did_not(tmp_path):
    """The actual gain: the result carries its origin.

    In the run without these tools `outputs: []` stayed, and none of the three files
    produced had a sidecar.
    """
    import os

    import geopandas as gpd
    from shapely.geometry import box

    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame({"x": [1]}, geometry=[box(0, 0, 4, 4)],
                     crs="EPSG:25832").to_file(tmp_path / "geocache" / "a.gpkg")
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    res = tools["vector_buffer"](input_path="a.gpkg", output_path="buf.gpkg", distance=10)
    assert res["ok"] is True and res["features_out"] == 1
    assert os.path.isfile(res["output"] + ".meta.json"), "Provenienz fehlt"


def test_the_guard_never_sends_a_ressort_to_a_tool_it_has_not_got():
    """The refusal points at a route the receiver can **reach**, not at a foreign one.

    Measured 2026-09-27 (`mean-elevation-per-district`): the orchestrator gave zonal
    statistics to the vector ressort, which has no `zonal_stats`. The guard forbade the
    snippet and said "call them directly, one call per step" — three times, about a tool
    this ressort cannot call. It then rebuilt zonal statistics by hand with
    `rasterio.mask(filled=True, nodata=0)`, averaging the zeros outside each polygon
    into the mean: eighteen districts, eighteen wrong elevations, `ok: true`. That is
    exactly what the checked operation prevents — and it was bound in the snippet, so it
    was reachable all along.
    """
    from chester.runtime.geopython import _refusal

    slice_ = frozenset({"vector_clip", "vector_reproject", "geo_python_run"})
    text = _refusal([("zonal_stats", "masks nodata out")], slice_)
    assert "already bound in this snippet" in text
    assert "a tool of the raster ressort" in text, "and say whose tool it is"
    assert "call them directly" not in text, "that advice is the dead end"


def test_read_vector_is_never_announced_as_a_tool():
    """`read_vector`/`write_vector` exist in **no** ressort and not in the single agent
    either — they are snippet bindings. For them the old sentence was always wrong,
    team or not."""
    from chester.runtime.geopython import _refusal

    for name in ("read_vector", "write_vector"):
        text = _refusal([(name, "collects the path spellings")], None)
        assert "already bound in this snippet" in text, name
        assert f"`{name}(...)` without importing it" in text, name


def test_the_single_agent_is_still_told_to_call_its_tools():
    """Whoever has the tool should call it — the fix must not dilute the normal case."""
    from chester.runtime.geopython import _refusal

    text = _refusal([("vector_buffer", "refuses a buffer in degrees")], None)
    assert "These are **tools**" in text
    assert "provenance" in text


def test_clipping_a_raster_is_not_a_zonal_statistic():
    """`rasterio.mask` on its own is a clip — and Chester has no tool for that.

    Measured 2026-09-27 (`terrain-ruggedness-index`): the raster ressort masked the DEM
    to the city boundary, entirely correctly, and was told "a checked function already
    does this: `zonal_stats`". There is nothing to redirect to — no tool clips a raster
    to a polygon — so the refusal cost a round and pointed nowhere. The ambiguous
    spelling now needs an aggregation in the same snippet, exactly as the `vector_merge`
    entry needs a `pd.concat` beside the read.
    """
    from chester.geo_python import hand_rolled_operations

    clip_only = ("from rasterio.mask import mask\n"
                 "out, tr = mask(src, [geom], crop=True)\n"
                 "dst.write(out)")
    assert hand_rolled_operations(clip_only) == []


def test_masking_with_an_average_is_still_caught():
    """The line between the two is the aggregation — and that is the defect itself.

    This snippet is what produced eighteen wrong district means on 2026-09-27: the zeros
    outside each polygon averaged into it. It must stay flagged.
    """
    from chester.geo_python import hand_rolled_operations

    zonal = ("from rasterio.mask import mask\n"
             "out, _ = mask(src, [g], crop=True, filled=True, nodata=0)\n"
             "values.append(float(out.mean()))")
    assert [n for n, _ in hand_rolled_operations(zonal)] == ["zonal_stats"]
    named = "stats = zonal_statistics(zones, raster)"
    assert [n for n, _ in hand_rolled_operations(named)] == ["zonal_stats"]
