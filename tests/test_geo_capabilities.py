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


def test_inspect_map_always_registered(tmp_path):
    # Always available, whoever ends up looking: a text-only main model gets the
    # snapshot routed to `model.vision_model` instead of the image (see below).
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    assert "inspect_map" in tools


def test_inspect_map_returns_snapshot_image(tmp_path):
    from pydantic_ai import BinaryContent, ToolReturn

    sample = write_building_sample(tmp_path)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    out = tools["inspect_map"](layers=[str(sample["buildings"])], question="check")
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
    out = tools["inspect_map"](
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
    out = tools["inspect_map"](layers=[str(sample["buildings"])], via_vision_model=True)
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
    out = tools["inspect_map"](layers=[str(sample["buildings"])])
    assert not isinstance(out, ToolReturn)  # i.e. no BinaryContent went out
    assert out["ok"] and out["review"] == "sieht plausibel aus"
    # And it says who looked, so the verdict is not mistaken for the caller's own.
    assert "ollama/qwen3-vl:latest" in out["note"]


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
    out = tools["inspect_map"](layers=[str(sample["buildings"])])
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
    out = tools["inspect_map"](layers=[str(sample["buildings"])])
    assert isinstance(out, ToolReturn)
    assert [c for c in out.content if isinstance(c, BinaryContent)]
