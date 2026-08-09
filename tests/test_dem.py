"""Offline tests for Copernicus GLO-30 tile addressing (no network)."""

from __future__ import annotations

from chester.capabilities.discovery import _DEM_BASE, _glo30_tile_name, _glo30_tiles


def test_tile_name_quadrants():
    assert _glo30_tile_name(50, 7) == "Copernicus_DSM_COG_10_N50_00_E007_00_DEM"
    assert _glo30_tile_name(-34, -58) == "Copernicus_DSM_COG_10_S34_00_W058_00_DEM"
    assert _glo30_tile_name(0, 0) == "Copernicus_DSM_COG_10_N00_00_E000_00_DEM"


def test_single_tile_bbox():
    tiles = _glo30_tiles([7.10, 50.73, 7.12, 50.74])
    assert len(tiles) == 1
    url, name = tiles[0]
    assert name == "Copernicus_DSM_COG_10_N50_00_E007_00_DEM"
    assert url == f"{_DEM_BASE}/{name}/{name}.tif"


def test_bbox_spanning_two_lon_tiles():
    names = {n for _, n in _glo30_tiles([6.6, 50.2, 7.4, 50.4])}
    assert names == {
        "Copernicus_DSM_COG_10_N50_00_E006_00_DEM",
        "Copernicus_DSM_COG_10_N50_00_E007_00_DEM",
    }


def test_bbox_spanning_2x2_grid():
    tiles = _glo30_tiles([6.5, 50.5, 7.5, 51.5])
    # lon {6,7} × lat {50,51} = 4 tiles
    assert len(tiles) == 4


def test_integer_edges_do_not_pull_extra_tiles():
    # bbox exactly [7,8]×[50,51] touches tile E007/N50 only, not E008/N51.
    names = {n for _, n in _glo30_tiles([7.0, 50.0, 8.0, 51.0])}
    assert names == {"Copernicus_DSM_COG_10_N50_00_E007_00_DEM"}
