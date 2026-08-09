"""Tests for the swisstopo connectors (chester/swisstopo.py) — the DACH extension.

Asset selection is covered offline; the swissALTI3D fetch (STAC + COG mosaic) is one
opt-in network test.
"""

from __future__ import annotations

import pytest

from chester import swisstopo


def test_alti3d_hrefs_picks_the_requested_resolution():
    feats = [{"assets": {
        "half": {"href": "https://x/t_0.5_2056_5728.tif", "gsd": 0.5},
        "two": {"href": "https://x/t_2_2056_5728.tif", "gsd": 2.0},
        "xyz": {"href": "https://x/t.xyz.zip", "gsd": 2.0},  # not a .tif → ignored
    }}]
    assert swisstopo._alti3d_hrefs(feats, 2.0) == ["https://x/t_2_2056_5728.tif"]
    assert swisstopo._alti3d_hrefs(feats, 0.5) == ["https://x/t_0.5_2056_5728.tif"]
    assert swisstopo._alti3d_hrefs(feats, 99) == []


def test_swissalti3d_tool_wired_in_discovery():
    from agent_build import _capability_tools
    from chester.capabilities import DataDiscoveryCapability

    tools = _capability_tools(DataDiscoveryCapability(workspace="/tmp/ws"))
    assert "fetch_swissalti3d" in tools


@pytest.mark.network
def test_fetch_swissalti3d_bern(tmp_path):
    import rasterio

    out = tmp_path / "bern.tif"
    r = swisstopo.fetch_swissalti3d([7.43, 46.94, 7.45, 46.95], str(out), resolution=2)
    assert r["ok"], r
    assert r["resolution_m"] == 2.0 and r["crs"] == "EPSG:2056" and r["tiles_used"] > 0
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 2056 and ds.res == (2.0, 2.0)


@pytest.mark.network
def test_fetch_swissalti3d_outside_switzerland_reports(tmp_path):
    # A bbox in Germany returns no swissALTI3D tiles (Switzerland only).
    r = swisstopo.fetch_swissalti3d([12.09, 49.01, 12.10, 49.02], str(tmp_path / "x.tif"))
    assert r["ok"] is False and "Switzerland only" in r["error"]


# ── swissBUILDINGS3D → CityJSON ──────────────────────────────────────────────────

def test_cityjson_from_solids_builds_multisurface():
    from chester import citymodel

    # one box-ish building: two triangular faces, z 0..10 → measured_height 10
    faces = [[(0, 0, 0), (10, 0, 0), (10, 10, 10)],
             [(0, 0, 0), (10, 10, 10), (0, 10, 10)]]
    cj = citymodel.cityjson_from_solids([("b1", {"measured_height": 10.0}, faces)],
                                        epsg=2056)
    assert cj["type"] == "CityJSON"
    assert cj["metadata"]["referenceSystem"].endswith("/2056")
    obj = cj["CityObjects"]["b1"]
    geom = obj["geometry"][0]
    assert geom["type"] == "MultiSurface" and geom["lod"] == "2"
    assert len(geom["boundaries"]) == 2  # two faces
    assert obj["attributes"]["measured_height"] == 10.0


def test_swissbuildings3d_tool_wired_in_citymodel():
    from agent_build import _capability_tools
    from chester.capabilities import GeoCityModelCapability

    tools = _capability_tools(GeoCityModelCapability(workspace="/tmp/ws"))
    assert "fetch_swissbuildings3d" in tools


@pytest.mark.network
def test_fetch_swissbuildings3d_bern(tmp_path):
    import json

    out = tmp_path / "bern.city.json"
    cache = tmp_path / "cache"
    r = swisstopo.fetch_swissbuildings3d([7.438, 46.947, 7.446, 46.952], str(out),
                                         str(cache), max_tiles=4)
    assert r["ok"], r
    assert r["crs"] == "EPSG:2056" and r["buildings"] > 0
    cj = json.loads(out.read_text())
    hs = [o["attributes"]["measured_height"] for o in cj["CityObjects"].values()
          if o.get("attributes")]
    assert hs and min(hs) > 0  # geometry-derived heights present and positive


@pytest.mark.network
def test_fetch_swissbuildings3d_outside_switzerland_reports(tmp_path):
    r = swisstopo.fetch_swissbuildings3d([12.09, 49.01, 12.10, 49.02],
                                         str(tmp_path / "x.json"), str(tmp_path))
    assert r["ok"] is False and "Switzerland only" in r["error"]


# ── swissBOUNDARIES3D → administrative boundaries ─────────────────────────────────

def test_swiss_boundary_levels_lists_the_four_levels():
    lvls = swisstopo.swiss_boundary_levels()
    codes = {r["level"] for r in lvls}
    assert codes == {"LAND", "KANTON", "BEZIRK", "GEMEINDE"}
    gem = next(r for r in lvls if r["level"] == "GEMEINDE")
    assert gem["key"] == "bfs_nummer"


def test_latest_gpkg_href_picks_the_newest_release():
    feats = [
        {"id": "swissboundaries3d_2024-01", "assets": {
            "g": {"href": "https://x/2024/b_2024-01_2056_5728.gpkg.zip"}}},
        {"id": "swissboundaries3d_2026-01", "assets": {
            "g": {"href": "https://x/2026/b_2026-01_2056_5728.gpkg.zip"},
            "s": {"href": "https://x/2026/b_2026-01_2056_5728.shp.zip"}}},
        {"id": "swissboundaries3d_2025-04", "assets": {
            "g": {"href": "https://x/2025/b_2025-04_2056_5728.gpkg.zip"}}},
    ]
    assert swisstopo._latest_gpkg_href(feats).endswith("b_2026-01_2056_5728.gpkg.zip")


def test_latest_gpkg_href_errors_without_gpkg_asset():
    with pytest.raises(RuntimeError):
        swisstopo._latest_gpkg_href([{"id": "x", "assets": {
            "s": {"href": "https://x/b.shp.zip"}}}])


def test_fetch_swissboundaries3d_unknown_level():
    r = swisstopo.fetch_swissboundaries3d("PROVINCE", "/tmp/x.gpkg", "/tmp/c")
    assert r["ok"] is False and "unknown Swiss level" in r["error"]


def test_resolve_kantonsnummer_digit_passthrough():
    # a numeric canton is returned as-is without touching the GeoPackage
    assert swisstopo._resolve_kantonsnummer("/nonexistent.gpkg", 2) == 2
    assert swisstopo._resolve_kantonsnummer("/nonexistent.gpkg", "2") == 2


def test_swiss_boundaries_tools_wired_in_boundaries_capability():
    from agent_build import _capability_tools
    from chester.capabilities import GeoBoundariesCapability

    tools = _capability_tools(GeoBoundariesCapability(workspace="/tmp/ws"))
    assert "swiss_boundaries_levels" in tools
    assert "fetch_swiss_boundaries" in tools


@pytest.mark.network
def test_fetch_swissboundaries3d_kanton_bern(tmp_path):
    import geopandas as gpd

    out = tmp_path / "bern_kanton.gpkg"
    r = swisstopo.fetch_swissboundaries3d("KANTON", str(out), str(tmp_path / "cache"),
                                          match="Bern")
    assert r["ok"], r
    assert r["crs"] == "EPSG:2056" and r["key_column"] == "kantonsnummer"
    assert r["units"] >= 1
    gdf = gpd.read_file(out)
    assert gdf.crs.to_epsg() == 2056
    assert any("Bern" in str(n) for n in gdf["name"])


@pytest.mark.network
def test_fetch_swissboundaries3d_gemeinde_has_population(tmp_path):
    import geopandas as gpd

    out = tmp_path / "gem.gpkg"
    # a small bbox around Bern city
    r = swisstopo.fetch_swissboundaries3d("GEMEINDE", str(out), str(tmp_path / "cache"),
                                          bbox_wgs84=[7.40, 46.93, 7.48, 46.97])
    assert r["ok"], r
    gdf = gpd.read_file(out)
    assert "bfs_nummer" in gdf.columns and "einwohnerzahl" in gdf.columns
    assert len(gdf) > 0


@pytest.mark.network
def test_fetch_swissboundaries3d_canton_selects_all_members(tmp_path):
    """canton='Bern' returns every Gemeinde of Kanton Bern (kantonsnummer 2),
    unlike match='Bern' which would only match units named 'Bern'."""
    import geopandas as gpd

    out = tmp_path / "bern_gem.gpkg"
    r = swisstopo.fetch_swissboundaries3d("GEMEINDE", str(out), str(tmp_path / "cache"),
                                          canton="Bern")
    assert r["ok"], r
    assert r["units"] > 200  # Kanton Bern has ~338 Gemeinden, not a handful
    gdf = gpd.read_file(out)
    assert set(gdf["kantonsnummer"].dropna().astype(int)) == {2}


# ── swissTLMRegio → topographic vector ────────────────────────────────────────────

def test_tlmregio_themes_lists_the_expected_themes():
    themes = {r["theme"] for r in swisstopo.tlmregio_themes()}
    assert {"roads", "railways", "buildings", "landcover", "rivers", "poi"} <= themes
    roads = next(r for r in swisstopo.tlmregio_themes() if r["theme"] == "roads")
    assert roads["layer"] == "tlmregio_transportation_road"


def test_fetch_swisstlmregio_unknown_theme():
    r = swisstopo.fetch_swisstlmregio("motorways", "/tmp/x.gpkg", "/tmp/c")
    assert r["ok"] is False and "unknown theme" in r["error"]


def test_fetch_swisstlmregio_tool_wired_in_discovery():
    from agent_build import _capability_tools
    from chester.capabilities import DataDiscoveryCapability

    tools = _capability_tools(DataDiscoveryCapability(workspace="/tmp/ws"))
    assert "fetch_swisstlmregio" in tools


@pytest.mark.network
def test_fetch_swisstlmregio_roads_bern(tmp_path):
    import geopandas as gpd

    out = tmp_path / "roads.gpkg"
    r = swisstopo.fetch_swisstlmregio("roads", str(out), str(tmp_path / "cache"),
                                      bbox_wgs84=[7.40, 46.93, 7.48, 46.97])
    assert r["ok"], r
    assert r["crs"] == "EPSG:2056" and r["theme"] == "roads" and r["features"] > 0
    gdf = gpd.read_file(out)
    assert gdf.crs.to_epsg() == 2056 and len(gdf) > 0
