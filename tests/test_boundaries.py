"""Tests for the administrative-boundaries connector (chester/boundaries.py).

Subset logic (GF=4 filter, key/name match, kept columns) is covered offline with a
synthetic vg250-schema GeoPackage; the real BKG download is one opt-in network test.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import box

from chester import boundaries as b


def _synthetic_gem(path):
    """A tiny vg250_gem-schema layer: 3 land units + 1 water variant (GF=1)."""
    rows = [
        {"AGS": "09162000", "GEN": "München", "BEZ": "Landeshauptstadt",
         "NUTS": "DE212", "GF": 4, "geometry": box(0, 0, 1, 1)},
        {"AGS": "09163000", "GEN": "Rosenheim", "BEZ": "Stadt",
         "NUTS": "DE213", "GF": 4, "geometry": box(2, 0, 3, 1)},
        {"AGS": "05315000", "GEN": "Köln", "BEZ": "Stadt",
         "NUTS": "DEA23", "GF": 4, "geometry": box(10, 10, 11, 11)},
        {"AGS": "09999999", "GEN": "Wasserfläche", "BEZ": "-",
         "NUTS": "", "GF": 1, "geometry": box(0, 0, 5, 5)},
    ]
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:25832")
    gdf.to_file(path, driver="GPKG", layer="vg250_gem")


def test_levels_catalog_covers_german_and_nuts_levels():
    codes = {row["level"] for row in b.levels_catalog()}
    assert {"STA", "LAN", "KRS", "GEM"} <= codes
    assert {"NUTS1", "NUTS2", "NUTS3"} <= codes
    # each level names its join key
    keys = {row["level"]: row["key"] for row in b.levels_catalog()}
    assert keys["GEM"] == "AGS" and keys["NUTS3"] == "NUTS_CODE"


def test_fetch_unknown_level_reports_before_download(tmp_path):
    r = b.fetch_boundaries("XXX", str(tmp_path / "o.gpkg"), str(tmp_path / "cache"))
    assert r["ok"] is False and "unknown level" in r["error"]


def test_fetch_land_only_drops_water_variant(tmp_path, monkeypatch):
    src_gpkg = tmp_path / "vg250.gpkg"
    _synthetic_gem(src_gpkg)
    monkeypatch.setattr(b, "_ensure_gpkg", lambda src, cache_dir: str(src_gpkg))

    r = b.fetch_boundaries("GEM", str(tmp_path / "out.gpkg"), str(tmp_path / "c"))
    assert r["ok"] and r["units"] == 3  # GF=1 water variant dropped
    assert r["key_column"] == "AGS"
    out = gpd.read_file(tmp_path / "out.gpkg")
    assert "AGS" in out.columns and "GF" not in out.columns  # keep only join cols


def test_fetch_match_by_key_prefix_and_by_name(tmp_path, monkeypatch):
    src_gpkg = tmp_path / "vg250.gpkg"
    _synthetic_gem(src_gpkg)
    monkeypatch.setattr(b, "_ensure_gpkg", lambda src, cache_dir: str(src_gpkg))

    # AGS prefix "09" → the two Bavarian units, not Köln (05…)
    r = b.fetch_boundaries("GEM", str(tmp_path / "by.gpkg"), str(tmp_path / "c"),
                           match="09")
    assert r["units"] == 2
    # name substring
    r2 = b.fetch_boundaries("GEM", str(tmp_path / "mu.gpkg"), str(tmp_path / "c"),
                            match="München")
    assert r2["units"] == 1


@pytest.mark.network
def test_fetch_boundaries_bkg_end_to_end(tmp_path):
    cache = str(tmp_path / "cache")
    # Bavaria Kreise: 71 Landkreise + 25 kreisfreie Städte = 96.
    r = b.fetch_boundaries("KRS", str(tmp_path / "by_krs.gpkg"), cache, match="09")
    assert r["ok"], r
    assert r["units"] == 96 and r["key_column"] == "AGS"
    # 16 Länder after the GF=4 land filter.
    r2 = b.fetch_boundaries("LAN", str(tmp_path / "lan.gpkg"), cache)
    assert r2["units"] == 16
    # NUTS path via the second dataset.
    r3 = b.fetch_boundaries("NUTS3", str(tmp_path / "n3.gpkg"), cache, match="DE21")
    assert r3["ok"] and r3["dataset"] == "nuts250"
