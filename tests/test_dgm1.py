"""Tests for the DGM1 (1 m terrain) connector — the fetch_dem sibling.

Tile derivation, the NRW index lookup, and the guards are covered offline; the
real state downloads + mosaic are one opt-in network test.
"""

from __future__ import annotations

import json
import os

import pytest

from chester import dgm1


def test_bayern_tiles_are_1km_geotiffs():
    tiles = dgm1._bayern_tiles([12.085, 49.010, 12.100, 49.022], "/tmp/_x")
    names = {n for _u, n in tiles}
    assert "726_5434.tif" in names
    assert all(u.startswith("https://download1.bayernwolke.de/a/dgm/dgm1/")
               and u.endswith(".tif") for u, _n in tiles)


def test_nrw_tiles_resolved_via_cached_index(tmp_path):
    # Seed the index cache so no network is needed: every covering tile maps to a
    # (year-suffixed) filename, which _nrw_tiles must turn into a URL.
    cache = tmp_path / "cache"
    cache.mkdir()
    bbox = [6.955, 50.940, 6.965, 50.945]
    grid = dgm1._grid_tiles(bbox, 1)
    mp = {f"{e}_{n}": f"dgm1_32_{e}_{n}_1_nw_2022.tif" for e, n in grid}
    (cache / "nrw_dgm1_index.json").write_text(json.dumps(mp))

    tiles = dgm1._nrw_tiles(bbox, str(cache))
    assert len(tiles) == len(grid)
    assert all(u.endswith("_1_nw_2022.tif") and "opengeodata.nrw.de" in u
               for u, _n in tiles)


def test_registry_wires_expected_states():
    assert {"BY", "NW", "BB", "MV"} <= set(dgm1.SOURCES)
    assert dgm1.SOURCES["BY"].mirrors  # Bayern has download mirrors
    assert dgm1.SOURCES["BB"].epsg == 25833  # eastern state → UTM33


def test_mv_dgm1_tiles_use_the_gtiff_elevation_variant():
    # MV's Atom feed has several formats; we must pick the real-elevation `gtiff`
    # variant (index=4), not the shaded/coded RGB ones.
    tiles = dgm1._mv_tiles([11.408, 53.625, 11.422, 53.635], "/tmp/_x")
    assert tiles and all(n.endswith("_2_gtiff.tif") for _u, n in tiles)
    assert all("index=4" in u and "geodaten-mv.de" in u for u, _n in tiles)


def test_brandenburg_dgm1_tiles_are_zipped_utm33():
    tiles = dgm1._brandenburg_tiles([13.055, 52.393, 13.070, 52.402], "/tmp/_x")
    assert tiles and all(u.endswith(".zip") and "geobasis-bb.de" in u
                         for u, _n in tiles)
    assert all(n.startswith("dgm_33") for _u, n in tiles)


def test_fetch_unknown_state_reports(tmp_path):
    r = dgm1.fetch_dgm1([12.0, 49.0, 12.1, 49.1], str(tmp_path / "o.tif"),
                        str(tmp_path / "c"), state="ZZ")
    assert r["ok"] is False and "not wired" in r["error"]


def test_fetch_refuses_oversized_bbox(tmp_path):
    # A country-scale bbox in Bayern derives far more than _MAX_TILES 1 m tiles —
    # it must refuse before downloading anything (deterministic, no network).
    r = dgm1.fetch_dgm1([11.0, 48.0, 13.0, 50.0], str(tmp_path / "o.tif"),
                        str(tmp_path / "c"), state="BY")
    assert r["ok"] is False and "narrow the bbox" in r["error"]
    assert not os.path.exists(tmp_path / "o.tif")


def test_the_refusal_names_the_way_out(tmp_path):
    """A limit without an alternative leaves the caller stuck.

    2026-08-24 (`terrain-ruggedness-index`): the agent asked for 1 m elevation over
    the whole Regensburg bbox, was told "narrow it — DGM1 at 1 m is heavy", and then
    spent minutes deciding what to do. For a ruggedness index over 13×13 km the 1 m
    model is ~750× finer than the question; GLO-30 answers it in about 1 MB. The
    refusal now says so, with both numbers.
    """
    r = dgm1.fetch_dgm1([11.0, 48.0, 13.0, 50.0], str(tmp_path / "o.tif"),
                        str(tmp_path / "c"), state="BY")
    assert "fetch_dem" in r["error"], "die Alternative muss beim Namen genannt sein"
    assert "GLO-30" in r["error"] and "GB" in r["error"] and "MB" in r["error"]
    assert "30 m is normally the right scale" in r["error"]


@pytest.mark.network
def test_fetch_dgm1_bayern_end_to_end(tmp_path):
    import rasterio

    out = tmp_path / "rgb.tif"
    r = dgm1.fetch_dgm1([12.085, 49.010, 12.100, 49.022], str(out),
                        str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["state"] == "BY" and r["resolution_m"] == 1
    with rasterio.open(out) as ds:
        assert ds.res == (1.0, 1.0)
        assert ds.crs.to_epsg() == 25832
        assert ds.nodata == -9999.0  # nodata preserved so slope/stats can mask it


@pytest.mark.network
def test_fetch_dgm1_nrw_via_index(tmp_path):
    r = dgm1.fetch_dgm1([6.955, 50.940, 6.965, 50.948], str(tmp_path / "k.tif"),
                        str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["state"] == "NW" and r["resolution_m"] == 1


@pytest.mark.network
def test_fetch_dgm1_mv_is_real_elevation(tmp_path):
    import rasterio

    out = tmp_path / "sn.tif"
    r = dgm1.fetch_dgm1([11.408, 53.625, 11.422, 53.635], str(out),
                        str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["state"] == "MV"
    with rasterio.open(out) as ds:
        assert ds.count == 1 and ds.dtypes[0] == "float32"  # elevation, not shaded RGB
        assert ds.crs.to_epsg() == 25833
