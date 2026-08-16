"""Tests for the DOP (aerial orthophoto) connector — the imagery fetch_dgm1 sibling.

Tile derivation, the NRW index lookup, the registry's honesty about Bayern and the
guards are covered offline; the real state downloads + mosaic are opt-in network
tests. The load-bearing claim — band 4 is near infrared, so NDVI works — is checked
on the real NRW download.
"""

from __future__ import annotations

import json
import os

import pytest

from chester import dop


def test_nw_tiles_resolved_via_cached_index(tmp_path):
    # Seed the index cache so no network is needed: the acquisition year is part of
    # the filename, so a tile can only be looked up, never derived.
    cache = tmp_path / "cache"
    cache.mkdir()
    bbox = [6.955, 50.940, 6.965, 50.945]
    grid = dop._grid_tiles(bbox, 1)
    mp = {f"{e}_{n}": f"dop10rgbi_32_{e}_{n}_1_nw_2025.jp2" for e, n in grid}
    (cache / "nw_dop10_index.json").write_text(json.dumps(mp))

    tiles = dop._nw_tiles(bbox, str(cache))
    assert len(tiles) == len(grid)
    assert all(u.endswith("_1_nw_2025.jp2") and "opengeodata.nrw.de" in u
               for u, _n in tiles)


def test_brandenburg_tiles_are_zipped_utm33():
    tiles = dop._bb_tiles([13.035, 52.400, 13.042, 52.404], "/tmp/_x")
    assert tiles and all(u.endswith(".zip") and "geobasis-bb.de" in u
                         for u, _n in tiles)
    assert all(n.startswith("dop_33") for _u, n in tiles)


def test_mv_tiles_use_the_rgbi_dataset():
    # The MV DOP20 feed carries an RGBI *and* an RGB dataset; the RGBI one is the
    # point of this connector (band 4 = NIR), so the id must not drift to RGB.
    tiles = dop._mv_tiles([11.408, 53.625, 11.422, 53.635], "/tmp/_x")
    assert tiles and all(n.startswith("dop20rgbi_33_") and n.endswith("_2_mv.tif")
                         for _u, n in tiles)
    assert all(dop._MV_DOP_DATASET in u and "geodaten-mv.de" in u for u, _n in tiles)


def test_bayern_tiles_carry_zone_prefix_and_data_segment():
    # Two traps that make Bayern *not* a copy of dgm1._bayern_tiles: the extra
    # `data/` path segment and the UTM-zone prefix in the filename. Guessing the
    # DGM1 layout here yields 404s, so pin both down.
    tiles = dop._by_tiles([12.095, 49.015, 12.101, 49.020], "/tmp/_x")
    names = {n for _u, n in tiles}
    assert "32726_5433.tif" in names, "zone prefix '32' missing from the tile name"
    assert all("/a/dop20/data/" in u for u, _n in tiles)


def test_registry_wires_expected_states():
    assert {"NW", "BB", "MV", "BY"} <= set(dop.SOURCES)
    assert all(s.status == "open" for s in dop.SOURCES.values())
    # NIR is what separates this from a WMS picture and makes spectral_index usable
    # at 10-20 cm. All but Bayern carry it (Bayern's CIR product has no derivable
    # per-tile URL) — so a source silently losing its I band must fail here.
    assert {c for c, s in dop.SOURCES.items() if s.bands == "RGBI"} == {"NW", "BB", "MV"}
    assert dop.SOURCES["BY"].bands == "RGB"
    assert dop.SOURCES["BB"].epsg == 25833 and dop.SOURCES["MV"].epsg == 25833
    assert dop.SOURCES["BY"].mirrors  # Bayern has download mirrors


def test_dop_sources_reports_resolution_and_nir():
    rows = {r["state"]: r for r in dop.dop_sources()}
    assert rows["NW"]["has_nir"] is True and rows["NW"]["resolution_m"] == 0.1
    assert rows["BY"]["has_nir"] is False  # RGB only — no NDVI in Bayern
    assert all(r["status"] == "open" for r in rows.values())


def test_documented_source_points_at_its_portal(tmp_path, monkeypatch):
    # No state is `documented` today, but the mechanism must keep working for the
    # next source that is open-but-not-fetchable (the recurring Chester case).
    stub = dop.DopSource("ZZ", "Testland", "n/a", resolution_m=0.2, resolver=None,
                         status="documented", portal="https://example.org/portal")
    monkeypatch.setitem(dop.SOURCES, "ZZ", stub)
    r = dop.fetch_dop([11.5, 48.1, 11.52, 48.12], str(tmp_path / "o.tif"),
                      str(tmp_path / "c"), state="ZZ")
    assert r["ok"] is False
    assert "stable per-tile URL" in r["error"]
    assert r["portal"] == "https://example.org/portal"


def test_fetch_unknown_state_reports(tmp_path):
    r = dop.fetch_dop([12.0, 49.0, 12.1, 49.1], str(tmp_path / "o.tif"),
                      str(tmp_path / "c"), state="ZZ")
    assert r["ok"] is False and "not registered" in r["error"]


def test_fetch_refuses_oversized_bbox(tmp_path):
    # DOP tiles are 30-80 MB each, so the cap is far stricter than DGM1's. Must
    # refuse before downloading anything (deterministic, no network).
    r = dop.fetch_dop([13.0, 52.3, 13.3, 52.6], str(tmp_path / "o.tif"),
                      str(tmp_path / "c"), state="BB")
    assert r["ok"] is False and "narrow it" in r["error"]
    assert not os.path.exists(tmp_path / "o.tif")


@pytest.mark.network
def test_fetch_dop_nrw_is_rgbi_and_ndvi_capable(tmp_path):
    """The real payoff: a 10 cm four-band image whose band 4 is truly NIR."""
    import rasterio

    out = tmp_path / "koeln.tif"
    r = dop.fetch_dop([6.955, 50.938, 6.963, 50.943], str(out),
                      str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["state"] == "NW" and r["resolution_m"] == 0.1
    assert r["bands"] == 4 and r["has_nir"] is True
    with rasterio.open(out) as ds:
        assert ds.count == 4 and ds.crs.to_epsg() == 25832
        assert ds.res == (0.1, 0.1)
        a = ds.read(out_shape=(4, 400, 400)).astype("float32")
    # Band 4 must behave like NIR, not like a fourth colour: over a mixed urban
    # scene NDVI has to span vegetation (>0.3) as well as sealed surface (<0).
    ndvi = (a[3] - a[0]) / (a[3] + a[0] + 1e-6)
    assert ndvi.max() > 0.3, "no vegetation signal — band 4 is probably not NIR"
    assert ndvi.min() < 0.0


@pytest.mark.network
def test_fetch_dop_brandenburg_unwraps_zipped_tile(tmp_path):
    import rasterio

    out = tmp_path / "potsdam.tif"
    r = dop.fetch_dop([13.035, 52.400, 13.042, 52.404], str(out),
                      str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["state"] == "BB" and r["resolution_m"] == 0.2
    with rasterio.open(out) as ds:
        assert ds.count == 4 and ds.crs.to_epsg() == 25833


@pytest.mark.network
def test_fetch_dop_mv_end_to_end(tmp_path):
    import rasterio

    out = tmp_path / "schwerin.tif"
    r = dop.fetch_dop([11.408, 53.625, 11.416, 53.630], str(out),
                      str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["state"] == "MV" and r["bands"] == 4
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 25833


@pytest.mark.network
def test_fetch_dop_bayern_is_rgb_without_nir(tmp_path):
    import rasterio

    out = tmp_path / "regensburg.tif"
    r = dop.fetch_dop([12.095, 49.015, 12.101, 49.020], str(out),
                      str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["state"] == "BY" and r["resolution_m"] == 0.2
    # Bayern is the one wired source without infrared — the result must say so,
    # so the agent does not plan an NDVI step that cannot work.
    assert r["bands"] == 3 and r["has_nir"] is False
    with rasterio.open(out) as ds:
        assert ds.count == 3 and ds.crs.to_epsg() == 25832


# ── acquisition year (the DOP follow-up, §5.11) ───────────────────────────────


def test_acquisition_year_is_read_where_the_source_states_it():
    """Only NRW puts the flight year in the tile name — the others must stay empty.

    For imagery, *when* it was taken is half the answer: a 2019 orthophoto still
    shows a building demolished in 2021. Guessing a year would be worse than having
    none, because it looks like knowledge.
    """
    assert dop.acquisition_years(
        ["dop10rgbi_32_280_5652_1_nw_2025.jp2",
         "dop10rgbi_32_281_5652_1_nw_2023.jp2"]) == [2023, 2025]
    assert dop.acquisition_years(["dop_33366-5807.zip", "32726_5433.tif"]) == []
    assert dop.acquisition_years(["dop20rgbi_33_206_5920_2_mv.tif"]) == []


def test_wms_backdrop_registry_is_separate_from_the_data_sources():
    """A backdrop is a picture, a fetch is data — two registries on purpose.

    One GetMap is ~70 KB; a single data tile is 18-91 MB. Using `fetch_dop` for a
    ground texture would be three orders of magnitude too expensive.
    """
    assert set(dop.WMS_BACKDROPS) <= set(dop.SOURCES)
    for url, layer in dop.WMS_BACKDROPS.values():
        assert url.startswith("https://") and layer


@pytest.mark.network
def test_aerial_backdrop_returns_an_image_inside_coverage_and_none_outside():
    inside = dop.aerial_backdrop_png([12.095, 49.015, 12.101, 49.020], 400, 300)
    assert inside and len(inside) > 4000
    # Vienna: no registered German service covers it — the caller falls back to OSM.
    assert dop.aerial_backdrop_png([16.37, 48.20, 16.38, 48.21], 400, 300) is None


@pytest.mark.network
def test_fetch_dop_reports_the_acquisition_year_for_nrw(tmp_path):
    r = dop.fetch_dop([6.955, 50.938, 6.960, 50.941], str(tmp_path / "y.tif"),
                      str(tmp_path / "cache"))
    assert r["ok"] and r["state"] == "NW"
    assert r["acquired_years"] and r["acquired"]
