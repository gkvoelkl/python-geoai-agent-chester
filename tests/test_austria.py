"""Tests for the Austrian boundaries connector (chester/austria.py, §5.10).

Level catalog + guards offline; the WFS fetch (SHAPE-ZIP over the certifi TLS path) is
network-gated.
"""

from __future__ import annotations

import pytest

from chester import austria


def test_austria_boundary_levels_lists_the_levels():
    codes = {r["level"] for r in austria.austria_boundary_levels()}
    assert {"GEM", "BEZIRK", "NUTS1", "NUTS2", "NUTS3"} == codes
    assert all(r["key"] == "g_id" for r in austria.austria_boundary_levels())


def test_fetch_austria_boundaries_unknown_level():
    r = austria.fetch_austria_boundaries("STATE", "/tmp/x.gpkg", "/tmp/c")
    assert r["ok"] is False and "unknown Austrian level" in r["error"]


def test_austria_boundaries_tools_wired_in_capability():
    from agent_build import _capability_tools
    from chester.capabilities import GeoBoundariesCapability

    tools = _capability_tools(GeoBoundariesCapability(workspace="/tmp/ws"))
    assert {"austria_boundaries_levels", "fetch_austria_boundaries"} <= set(tools)


@pytest.mark.network
def test_fetch_austria_boundaries_nuts2(tmp_path):
    import geopandas as gpd

    out = tmp_path / "at_nuts2.gpkg"
    r = austria.fetch_austria_boundaries("NUTS2", str(out), str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["crs"] == "EPSG:31287" and r["key_column"] == "g_id"
    assert r["units"] == 9  # Austria's nine NUTS-2 regions
    gdf = gpd.read_file(out)
    assert gdf.crs.to_epsg() == 31287 and set(gdf.columns) >= {"g_id", "g_name"}


@pytest.mark.network
def test_fetch_austria_boundaries_gemeinden_by_bundesland_prefix(tmp_path):
    """match='7' selects all Tirol Gemeinden — GKZ is hierarchical (1st digit = Land)."""
    import geopandas as gpd

    out = tmp_path / "tirol_gem.gpkg"
    r = austria.fetch_austria_boundaries("GEM", str(out), str(tmp_path / "cache"),
                                         match="7")
    assert r["ok"], r
    assert r["units"] > 250  # Tirol has ~277 Gemeinden
    gdf = gpd.read_file(out)
    assert gdf["g_id"].astype(str).str.startswith("7").all()


# ── DGM (BEV ALS 1 m) ──────────────────────────────────────────────────────────────

def test_austria_dem_tool_wired_in_discovery():
    from agent_build import _capability_tools
    from chester.capabilities import DataDiscoveryCapability

    tools = _capability_tools(DataDiscoveryCapability(workspace="/tmp/ws"))
    assert "fetch_austria_dem" in tools


@pytest.mark.network
def test_fetch_austria_dem_vienna(tmp_path):
    import rasterio

    out = tmp_path / "at_dem.tif"
    # a small bbox in Vienna (well within Austria / BEV coverage)
    r = austria.fetch_austria_dem([16.36, 48.20, 16.38, 48.21], str(out),
                                  str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["crs"] == "EPSG:3035" and r["resolution_m"] == 1.0 and r["tiles_used"] > 0
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 3035


@pytest.mark.network
def test_fetch_austria_dem_outside_austria_reports(tmp_path):
    r = austria.fetch_austria_dem([2.35, 48.85, 2.36, 48.86], str(tmp_path / "x.tif"),
                                  str(tmp_path / "cache"))  # Paris
    assert r["ok"] is False and "Austria only" in r["error"]


# ── Vienna LOD2.1 buildings → CityJSON ──────────────────────────────────────────────

def test_vienna_buildings_tool_wired_in_citymodel():
    from agent_build import _capability_tools
    from chester.capabilities import GeoCityModelCapability

    tools = _capability_tools(GeoCityModelCapability(workspace="/tmp/ws"))
    assert "fetch_vienna_buildings" in tools


def test_fetch_vienna_buildings_bad_source(tmp_path):
    r = austria.fetch_vienna_buildings(str(tmp_path / "x.json"), str(tmp_path / "c"),
                                       source="/no/such/file.gml")
    assert r["ok"] is False


@pytest.mark.network
def test_fetch_vienna_buildings_sample(tmp_path):
    import json

    out = tmp_path / "vienna.city.json"
    r = austria.fetch_vienna_buildings(str(out), str(tmp_path / "cache"), source="sample")
    assert r["ok"], r
    assert r["crs"] == "EPSG:31256" and (r.get("buildings") or 0) > 0
    cj = json.loads(out.read_text())
    assert cj["type"] == "CityJSON" and cj["CityObjects"]
