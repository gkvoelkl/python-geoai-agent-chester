"""Pure unit tests for the geodata-search / OSM helpers (no network).

Covers the deterministic building blocks behind geodata_search, fetch_vector and
resource classification, WFS URL splitting, CKAN-dialect field
extraction, format inference, the case-collision save fix, and the Overpass
retry/mirror logic (mocked).
"""

from __future__ import annotations

from _util import tools_of

from chester.capabilities.discovery import (
    _area_match_warning,
    _classify_resource,
    _dataset_license,
    _or_tags_warning,
    _photon_bbox,
    _publisher,
    _quoted_boolean_hint,
    _resource_url,
    _saveable,
    _stringify_tags,
    _vector_suffix,
    _wfs_base_and_typename,
)

# ── _stringify_tags (osmnx rejects int/float tag values) ─────────────────────


def test_stringify_tags_coerces_numeric_values():
    # The model naturally writes admin_level as an int; osmnx needs a str.
    out = _stringify_tags({"boundary": "administrative", "admin_level": 8})
    assert out == {"boundary": "administrative", "admin_level": "8"}


def test_stringify_tags_preserves_bool_and_lists():
    out = _stringify_tags({"building": True, "layer": -1, "ref": ["A", "B"],
                           "levels": [1, 2], "floor": 2.0})
    assert out["building"] is True          # bool stays a bool (not "True")
    assert out["layer"] == "-1"
    assert out["ref"] == ["A", "B"]
    assert out["levels"] == ["1", "2"]      # list of ints → list of strs
    assert out["floor"] == "2"              # whole float → int-style string


# ── _quoted_boolean_hint ("highway": "true" is not "highway": true) ───────────


def test_quoted_boolean_hint_names_the_key_and_the_fix():
    """The two-quotation-mark slip that cost `walk-isochrone-hauptbahnhof` its network.

    `{"highway": "true"}` asks for ways literally tagged `highway=true` — none
    exist. Overpass answered empty, the error said "Check query location, tags,
    and log", and the agent rebuilt the walking network from `footway` + `path`:
    635 km of Regensburg's 1844 km of walkable ways, every residential street
    missing. The isochrone that followed looked entirely plausible.
    """
    hint = _quoted_boolean_hint({"highway": "true"})
    assert '"highway": "true"' in hint      # names what was sent
    assert '"highway": true' in hint        # and what to send instead


def test_quoted_boolean_hint_stays_silent_on_correct_tags():
    """It must not fire on a real value — `name=truelove` is not a mistake."""
    for tags in ({"highway": True}, {"natural": "water"}, {"admin_level": "8"},
                 {"name": "truelove"}, {"building": ["yes", "house"]}):
        assert _quoted_boolean_hint(tags) == "", tags


def test_quoted_boolean_hint_is_a_note_not_a_rewrite():
    """`"true"` is a legal tag value: report it, never silently change the query.

    A connector that rewrites what it was asked stops being trustworthy about what
    it actually asked, which is worse than an unhelpful empty answer.
    """
    tags = {"highway": "true"}
    _quoted_boolean_hint(tags)
    assert tags == {"highway": "true"}      # untouched
    assert _stringify_tags(tags) == {"highway": "true"}  # and not coerced elsewhere

# The real (mislabelled) Regensburg catalog resource: tagged "CSV", truly a WFS.
_RGBG_WFS = (
    "https://mapservice.regensburg.de/cgi-bin/mapserv?map=/data/ows/maps/"
    "kleingliederung_wfs.map&VERSION=1.0.0&outputformat=csv&SERVICE=WFS&"
    "REQUEST=GetFeature&TYPENAME=kleingliederung"
)


# ── _classify_resource ───────────────────────────────────────────────────────


def test_classify_unmangles_wfs_mislabelled_as_csv():
    # The whole point: trust the URL (SERVICE=WFS), not CKAN's "CSV" label.
    r = _classify_resource(_RGBG_WFS, "CSV")
    assert r["service"] == "WFS"
    assert r["typename"] == "kleingliederung"
    # base URL keeps MapServer's map= param, drops the request-specific ones.
    assert "map=" in r["wfs_url"]
    assert "REQUEST" not in r["wfs_url"] and "TYPENAME" not in r["wfs_url"]


def test_classify_wms_from_url():
    r = _classify_resource("https://x/ows?SERVICE=WMS&REQUEST=GetMap", "png")
    assert r["service"] == "WMS"


def test_classify_by_extension():
    assert _classify_resource("https://x/data.geojson", None)["service"] == "GeoJSON"
    assert _classify_resource("https://x/roads.zip", None)["service"] == "Shapefile"
    assert _classify_resource("https://x/a.gpkg", None)["service"] == "GeoPackage"
    assert _classify_resource("https://x/a.gml", None)["service"] == "GML"


def test_classify_by_format_hint_when_url_opaque():
    # No extension, no SERVICE= — fall back to the format string.
    r = _classify_resource("https://x/download?id=7", "GeoJSON")
    assert r["service"] == "GeoJSON"


def test_classify_unknown_keeps_format_label():
    assert _classify_resource("https://x/download?id=7", "XYZ")["service"] == "XYZ"


# ── _wfs_base_and_typename ───────────────────────────────────────────────────


def test_wfs_base_and_typename_splits_request():
    base, typename = _wfs_base_and_typename(_RGBG_WFS)
    assert typename == "kleingliederung"
    for stripped in ("service=", "request=", "typename=", "outputformat=", "version="):
        assert stripped not in base.lower()
    assert "map=" in base  # a non-request param is preserved


def test_wfs_base_plain_service_has_empty_query():
    base, typename = _wfs_base_and_typename(
        "https://geo.example/wfs?SERVICE=WFS&REQUEST=GetFeature&TYPENAME=x:y"
    )
    assert typename == "x:y"
    assert base.rstrip("?") == "https://geo.example/wfs"


def test_wfs_base_strips_pasted_wms_version():
    # Regression: a caller (or the LLM) pastes a WMS-style version=1.3.0 into a WFS
    # URL; it must be stripped so it can't fight owslib's WFS version negotiation.
    base, _ = _wfs_base_and_typename(
        "https://host/arcgis/services/x/MapServer/WFSServer"
        "?service=WFS&version=1.3.0&request=GetCapabilities"
    )
    assert "1.3.0" not in base and "version=" not in base.lower()
    assert base.rstrip("?").endswith("/WFSServer")


# ── _resource_url (CKAN dialects) ────────────────────────────────────────────


def test_resource_url_govdata_style():
    assert _resource_url({"url": "https://x/a.geojson"}) == "https://x/a.geojson"


def test_resource_url_dataeuropa_bracketed_access_url():
    # data.europa.eu wraps the URL in [...] under access_url.
    res = {"url": None, "access_url": "[https://x/a.geojson]"}
    assert _resource_url(res) == "https://x/a.geojson"


def test_resource_url_none_when_absent():
    assert _resource_url({"format": "CSV"}) is None


# ── _publisher / _dataset_license ────────────────────────────────────────────


def test_publisher_plain_string():
    assert _publisher({"organization": {"title": "GovData"}}) == "GovData"


def test_publisher_language_map_prefers_en_then_de():
    assert _publisher({"organization": {"title": {"de": "Amt", "en": "Office"}}}) == "Office"
    assert _publisher({"organization": {"title": {"de": "Amt"}}}) == "Amt"


def test_publisher_missing():
    assert _publisher({}) is None


def test_license_dataset_level():
    assert _dataset_license({"license_title": "CC-BY-4.0"}, []) == "CC-BY-4.0"


def test_license_falls_back_to_resource_level():
    ds = {"license_id": None}
    resources = [{"license": "dl-de/by-2-0"}]
    assert _dataset_license(ds, resources) == "dl-de/by-2-0"


def test_license_none_when_unstated():
    assert _dataset_license({}, [{"format": "CSV"}]) is None


# ── _vector_suffix ───────────────────────────────────────────────────────────


def test_vector_suffix_from_extension():
    assert _vector_suffix("https://x/a.geojson", "") == ".geojson"
    assert _vector_suffix("https://x/a.json", "") == ".geojson"  # normalised
    assert _vector_suffix("https://x/a.zip", "") == ".zip"
    assert _vector_suffix("https://x/a.gml", "") == ".gml"


def test_vector_suffix_from_content_type_when_no_extension():
    url = "https://x/cgi-bin/mapserv?SERVICE=WFS&REQUEST=GetFeature"
    assert _vector_suffix(url, "application/json") == ".geojson"
    assert _vector_suffix(url, "application/gml+xml") == ".gml"
    assert _vector_suffix(url, "application/zip") == ".zip"


# ── _saveable (case-collision merge + list stringify) ────────────────────────


def _gdf_with_collision():
    import geopandas as gpd
    from shapely.geometry import Point

    return gpd.GeoDataFrame(
        {
            "name": ["a", "b", "c"],
            "fixme": [None, "lower", None],
            "FIXME": ["upper", None, None],
            "nodes": [[1, 2], [3], [4, 5]],
        },
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
        crs="EPSG:4326",
    )


def test_saveable_merges_case_colliding_columns():
    import pandas as pd

    out = _saveable(_gdf_with_collision())
    # One merged column survives (first-seen name wins), the duplicate is gone.
    cols = [c for c in out.columns if c.lower() == "fixme"]
    assert cols == ["fixme"]
    # first-seen wins where present, the other fills the null; row 3 stays empty.
    merged = out["fixme"].tolist()
    assert merged[:2] == ["upper", "lower"]
    assert pd.isna(merged[2])


def test_saveable_stringifies_list_cells():
    out = _saveable(_gdf_with_collision())
    assert out["nodes"].tolist() == ["1;2", "3", "4;5"]


def test_saveable_writes_to_geopackage(tmp_path):
    # The original failure: fixme/FIXME collide and GPKG (SQLite) rejects the dup.
    import geopandas as gpd

    p = tmp_path / "out.gpkg"
    _saveable(_gdf_with_collision()).to_file(p)
    assert len(gpd.read_file(p)) == 3


# ── geocode: point-sized match warning + Photon fallback ─────────────────────


def test_area_match_warning_fires_on_the_two_real_wrong_hits():
    """Both bad geocodes of 2026-08-25 gave `ok: true` and a right-sounding name.

    "Regensburger Altstadt" → a Regensburger Straße in **Passau**; "Regensburg
    Altstadt, Deutschland" → the **Arbeitsgericht**. Nothing in the reply said so
    except the size: area_km2 was 0.0 for both. Clipping a city analysis to either
    bbox silently answers a question about one address.
    """
    passau = _area_match_warning(
        "Regensburger Straße, Altstadt, Ries, Passau, Bavaria, Germany",
        0.0, "highway", "primary")
    court = _area_match_warning(
        "Arbeitsgericht Regensburg, Bertoldstraße, Altstadt, Regensburg, Germany",
        0.0, "amenity", "courthouse")
    for w in (passau, court):
        assert "point-sized" in w
    assert "highway=primary" in passau      # names what kind of thing it matched
    assert "amenity=courthouse" in court


def test_area_match_warning_silent_for_a_real_place():
    """Regensburg itself: 145 km², boundary=administrative — no warning."""
    assert _area_match_warning("Regensburg, Bavaria, Germany", 145.0,
                               "boundary", "administrative") == ""
    assert _area_match_warning("Altstadt, Regensburg", 2.4, "place", "suburb") == ""
    assert _area_match_warning("nowhere", None, None, None) == ""  # unknown ≠ wrong


def test_photon_bbox_reorders_the_corners():
    """Photon says [west, north, east, south]; Chester's bbox is [w, s, e, n].

    Passing it through unchanged would put every south edge above its north edge —
    an empty rectangle made of four entirely plausible numbers.
    """
    # the real extent Photon returns for Regensburg
    assert _photon_bbox([12.0290745, 49.0764158, 12.1916078, 48.9667457]) == [
        12.029075, 48.966746, 12.191608, 49.076416
    ]
    w, s, e, n = _photon_bbox([12.0290745, 49.0764158, 12.1916078, 48.9667457])
    assert s < n and w < e
    assert _photon_bbox(None) is None and _photon_bbox([1, 2]) is None


def test_geocode_falls_back_to_photon_when_nominatim_finds_nothing(monkeypatch, tmp_path):
    """`Regensburger Hauptbahnhof` — Nominatim returns nothing at all, Photon has it.

    Nominatim parses addresses and cannot split the name; Photon indexes OSM names
    and answers `railway=station`. The reply must say which source spoke and that
    no boundary came with it.
    """
    import osmnx._nominatim as nom

    from chester.capabilities import discovery

    monkeypatch.setattr(nom, "_download_nominatim_element", lambda *a, **k: [])
    monkeypatch.setattr(discovery, "_photon_lookup", lambda *a, **k: [{
        "display_name": "Regensburg Hauptbahnhof, Regensburg, Bayern, Deutschland",
        "class": "railway", "type": "station",
        "centroid": [12.0997, 49.0122], "bbox": None,
    }])
    tools = tools_of(discovery.DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["geocode"](query="Regensburger Hauptbahnhof")

    assert r["ok"] and r["source"] == "photon"
    assert r["match_class"] == "railway" and r["match_type"] == "station"
    assert r["centroid"] == [12.0997, 49.0122]
    assert r["boundary"] is None and "NO boundary" in r["note"]


def test_geocode_photon_failure_is_not_fatal(monkeypatch):
    """An unreachable second opinion must not turn a Nominatim miss into a crash."""
    import requests

    from chester.capabilities import discovery

    def _offline(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(requests, "get", _offline)
    assert discovery._photon_lookup("anything") == []


# ── _or_tags_warning (several tag keys are a union, not an intersection) ─────


def _frame(rows):
    import geopandas as gpd
    from shapely.geometry import Point

    return gpd.GeoDataFrame(rows, geometry=[Point(0, i) for i in range(len(rows))],
                            crs="EPSG:4326")


def test_or_tags_warning_fires_on_the_documented_boundary_query():
    """The query Chester's own docstring recommended — and what it really returns.

    Measured on Regensburg: `boundary=administrative` alone 41 features,
    `admin_level=8` alone 21, both together **42** (the union; the intersection is
    20). In `voronoi-catchment` (2026-08-26) the agent followed that documentation
    and clipped a city analysis against a layer holding the Landkreis, the Bezirk
    and nine neighbouring municipalities — 24.9 km² of Regensburg ended up in no
    catchment at all.
    """
    gdf = _frame([
        {"boundary": "administrative", "admin_level": "8"},   # matches both
        {"boundary": "administrative", "admin_level": "6"},   # Landkreis — union only
        {"boundary": "administrative", "admin_level": None},  # no level at all
    ])
    w = _or_tags_warning(gdf, {"boundary": "administrative", "admin_level": "8"})
    assert "2 of 3" in w
    assert "OR, not AND" in w
    assert "where=" in w and "'admin_level': '8'" in w  # names the intersecting fix


def test_or_tags_warning_silent_when_every_row_matches():
    gdf = _frame([{"boundary": "administrative", "admin_level": "8"},
                  {"boundary": "administrative", "admin_level": "8"}])
    assert _or_tags_warning(gdf, {"boundary": "administrative", "admin_level": "8"}) == ""


def test_or_tags_warning_silent_for_a_single_tag():
    """One key cannot be a union — the warning must not fire on the common case."""
    gdf = _frame([{"building": "yes"}, {"building": "house"}])
    assert _or_tags_warning(gdf, {"building": True}) == ""
    assert _or_tags_warning(gdf, {"building": "yes"}) == ""


def test_or_tags_warning_treats_true_as_key_present():
    """`{"k": true}` means "any value of k", so only a missing key is a mismatch."""
    gdf = _frame([{"highway": "footway", "surface": "gravel"},
                  {"highway": "path", "surface": None}])
    w = _or_tags_warning(gdf, {"highway": True, "surface": True})
    assert "1 of 2" in w
