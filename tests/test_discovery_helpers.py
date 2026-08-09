"""Pure unit tests for the geodata-search / OSM helpers (no network).

Covers the deterministic building blocks behind geodata_search, fetch_vector and
osm_query_raw: resource classification, WFS URL splitting, CKAN-dialect field
extraction, format inference, the case-collision save fix, and the Overpass
retry/mirror logic (mocked).
"""

from __future__ import annotations

import chester.capabilities.discovery as d
from chester.capabilities.discovery import (
    _classify_resource,
    _dataset_license,
    _overpass_request,
    _publisher,
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


# ── _overpass_request (retry + mirror fallback, mocked) ──────────────────────


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {"elements": []}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))


def test_overpass_retries_then_falls_back_to_mirror(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        if "primary" in url:
            return _Resp(504)  # transient → retry, then mirror
        return _Resp(200, {"elements": [{"type": "node", "id": 1}]})

    import time

    import requests

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    out = _overpass_request("q", {}, ["http://primary/i", "http://mirror/i"])
    assert out["elements"] == [{"type": "node", "id": 1}]
    # primary tried twice (attempts=2), then the mirror.
    assert [c for c in calls] == ["http://primary/i", "http://primary/i", "http://mirror/i"]


def test_overpass_fails_fast_on_bad_query(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        return _Resp(400)  # QL syntax error → do NOT retry or try mirrors

    import requests

    monkeypatch.setattr(requests, "post", fake_post)

    try:
        _overpass_request("bad", {}, ["http://primary/i", "http://mirror/i"])
        assert False, "expected an HTTPError"
    except requests.HTTPError:
        pass
    assert calls == ["http://primary/i"]
