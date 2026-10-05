"""Tests for the administrative-boundaries connector (chester/boundaries.py).

Subset logic (GF=4 filter, key/name match, kept columns) is covered offline with a
synthetic vg250-schema GeoPackage; the real BKG download is one opt-in network test.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import box

from chester import boundaries as b
from chester.capabilities.boundaries import GeoBoundariesCapability


def _tools_with_fake_fetch(monkeypatch, tmp_path, fake):
    """The capability's tools with the network layer replaced.

    The inference loop is what these tests are about — which levels it tries, in
    which order, and when it stops. Downloading the real BKG datasets would test
    the BKG instead, slowly and unrepeatably. `monkeypatch` and not a try/finally:
    the patch has to outlive this function, or the tool runs against the real thing
    (which is exactly what happened on the first attempt).
    """
    # Since Phase KM step 1 the tools live in the wrapper layer; so the patch goes there,
    # no longer onto the capability.
    import chester.boundariestools as mod

    monkeypatch.setattr(mod.boundaries, "fetch_boundaries", fake)
    toolset = GeoBoundariesCapability(workspace=str(tmp_path)).get_toolset()
    return {name: getattr(t, "function", t) for name, t in toolset.tools.items()}


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


# ── Friction gradient between geocode and fetch_boundaries ──────────────
#
# Measured over all sessions: `geocode` 131 calls, `fetch_boundaries` 7,
# `boundaries_levels` 2. The prompt gives the official source 3,620 characters and is not
# followed — because the wrong route costs one word (`geocode("Tegernheim")`) and the
# right one a foreign taxonomy plus two required arguments. Two levers: `level` optional
# (below), and a nudge in the geocode result (test_discovery).


def test_level_is_optional_and_inferred_from_the_name(monkeypatch, tmp_path):
    """`fetch_boundaries(out, match="Tegernheim")` should work without a level."""
    calls = []

    def fake(level, output_path, cache_dir, match=None, bbox=None, land_only=True):
        calls.append(level)
        if level != "GEM":
            return {"ok": False, "error": f"no {level} units matched '{match}'"}
        return {"ok": True, "level": "GEM", "units": 1, "key_column": "AGS"}

    tools = _tools_with_fake_fetch(monkeypatch, tmp_path, fake)
    r = tools["fetch_boundaries"]("t.gpkg", match="Tegernheim")
    assert r["ok"] and r["level"] == "GEM"
    assert r["level_inferred"] is True
    assert calls == ["GEM"], "die kleinste Einheit zuerst — sonst wird eskaliert"


def test_inference_escalates_upward_until_something_matches(monkeypatch, tmp_path):
    def fake(level, output_path, cache_dir, match=None, bbox=None, land_only=True):
        if level in ("GEM", "VWG"):
            return {"ok": False, "error": f"no {level} units matched '{match}'"}
        return {"ok": True, "level": level, "units": 1}

    tools = _tools_with_fake_fetch(monkeypatch, tmp_path, fake)
    r = tools["fetch_boundaries"]("k.gpkg", match="Regensburg")
    assert r["ok"] and r["level"] == "KRS"
    assert r["levels_tried"] == ["GEM", "VWG"]


def test_a_download_failure_never_becomes_a_wrong_level(monkeypatch, tmp_path):
    """The error that happened while building: a cold GEM download failed, the loop took
    that as 'not on this level' and returned Tegernheim as a Verwaltungsgemeinschaft —
    plausible, well-formed, wrong."""
    def fake(level, output_path, cache_dir, match=None, bbox=None, land_only=True):
        if level == "GEM":
            return {"ok": False, "error": "download failed: TimeoutError: timed out"}
        return {"ok": True, "level": level, "units": 1}

    tools = _tools_with_fake_fetch(monkeypatch, tmp_path, fake)
    r = tools["fetch_boundaries"]("t.gpkg", match="Tegernheim")
    assert r["ok"] is False, "ein Downloadfehler darf nicht zur nächsten Ebene führen"
    assert "stopped at GEM" in r["error"]
    assert "download failed" in r["error"]


def test_inference_without_a_match_is_refused(monkeypatch, tmp_path):
    """Without a filter GEM would first load all of Germany in order to then 'match'."""
    tools = _tools_with_fake_fetch(monkeypatch, tmp_path, lambda *a, **k: {"ok": True})
    r = tools["fetch_boundaries"]("x.gpkg")
    assert r["ok"] is False and "needs `match`" in r["error"]


def test_an_explicit_level_skips_inference_entirely(monkeypatch, tmp_path):
    calls = []

    def fake(level, output_path, cache_dir, match=None, bbox=None, land_only=True):
        calls.append(level)
        return {"ok": True, "level": level, "units": 96}

    tools = _tools_with_fake_fetch(monkeypatch, tmp_path, fake)
    r = tools["fetch_boundaries"]("b.gpkg", match="09", level="KRS")
    assert calls == ["KRS"]
    assert "level_inferred" not in r


# ── Der Kanton-gegen-Name-Fehlgriff ─────────────────────────────────────


def test_match_without_canton_is_flagged_at_gemeinde_level():
    """Measured 2026-09-05, `swiss-population-choropleth-bern`.

    The agent called `fetch_swiss_boundaries(level="GEMEINDE", match="Bern")` without
    `canton`, got **4 units** — the municipalities *named* Bern, across all cantons — and
    rendered them as "Einwohnerzahl je Gemeinde im Kanton Bern". The number was in the
    result; nobody looked. The run before had got it right: a coin flip, not a lack of
    knowledge.
    """
    from chester.boundariestools import _canton_confusion_warning

    w = _canton_confusion_warning("GEMEINDE", "Bern", None, 4)
    assert "4 unit(s)" in w, "die Zahl ist der eigentliche Hinweis"
    assert "canton=" in w
    assert "NOT hierarchical" in w


def test_no_warning_when_canton_was_used():
    """Counter-check — the right call must not be admonished."""
    from chester.boundariestools import _canton_confusion_warning

    assert not _canton_confusion_warning("GEMEINDE", None, "Bern", 338)
    assert not _canton_confusion_warning("GEMEINDE", "Bern", "Bern", 1)


def test_no_warning_where_the_trap_does_not_exist():
    """On KANTON level `match="Bern"` is exactly right — there is nothing below it to
    confuse."""
    from chester.boundariestools import _canton_confusion_warning

    assert not _canton_confusion_warning("KANTON", "Bern", None, 1)
    assert not _canton_confusion_warning("LAND", "Schweiz", None, 1)
