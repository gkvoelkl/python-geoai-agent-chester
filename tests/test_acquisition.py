"""Offline tests for the §5.5 acquisition layer (multi-catalog STAC + WFS)."""

from __future__ import annotations

from _util import tools_of

from chester.capabilities.discovery import DataDiscoveryCapability, _maybe_sign


def test_stac_unknown_catalog_errors_without_network(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["stac_search"](
        bbox=[7.0, 50.5, 7.1, 50.6], datetime="2021-07-01/2021-07-31", catalog="nope"
    )
    assert r["ok"] is False
    # the built-in catalogs are listed so the model can correct itself
    assert "earth-search" in r["error"] and "planetary-computer" in r["error"]


def test_configured_catalog_merges_into_registry(tmp_path):
    cap = DataDiscoveryCapability(
        workspace=str(tmp_path),
        stac_catalogs={"my-cat": {"url": "https://example.com/stac", "sign": False}},
    )
    tools = tools_of(cap)
    # An unknown catalog lists the merged registry, proving the override took.
    r = tools["stac_search"](bbox=[0, 0, 1, 1], datetime="2021", catalog="still-wrong")
    assert "my-cat" in r["error"]


def test_maybe_sign_passes_through_non_pc_urls():
    plain = "https://example.com/data/scene_nir.tif"
    assert _maybe_sign(plain) == plain  # not a PC blob URL → unchanged


def test_wfs_bad_url_is_reported_not_raised(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["wfs_features"](
        url="http://127.0.0.1:1/wfs", typename="x", output_path="o.geojson"
    )
    assert r["ok"] is False and "error" in r


def test_wms_bad_url_is_reported_not_raised(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["wms_capabilities"](url="http://127.0.0.1:1/wms")
    assert r["ok"] is False and "error" in r
    r = tools["fetch_wms_map"](
        url="http://127.0.0.1:1/wms", layer="x",
        bbox=[12.0, 49.0, 12.1, 49.1], output_path="o.tif",
    )
    assert r["ok"] is False and "error" in r


def test_wms_tools_are_wired(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    assert "fetch_wms_map" in tools and "wms_capabilities" in tools


def _tile_index(crs="EPSG:25832"):
    import geopandas as gpd
    from shapely.geometry import box

    return gpd.GeoDataFrame(
        {"tile_id": ["a", "b", "c"],
         "download": ["https://ot.example/a.laz", "https://ot.example/b.laz",
                      "https://ot.example/c.laz"]},
        geometry=[box(0, 0, 1000, 1000), box(2000, 2000, 3000, 3000),
                  box(4000, 4000, 5000, 5000)],
        crs=crs,
    )


def test_tile_index_url_field_autodetected():
    from chester.capabilities.discovery import _find_url_field

    assert _find_url_field(_tile_index()) == "download"


def test_select_tile_urls_intersects_and_reprojects():
    from chester.capabilities.discovery import _select_tile_urls

    # a WGS84 bbox over the projected origin tile 'a' selects only that tile
    urls, field = _select_tile_urls(_tile_index(), [4.5112, -0.001, 4.5125, 0.02])
    assert field == "download" and urls == ["https://ot.example/a.laz"]


def test_fetch_pointcloud_errors_without_url_field(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box

    # a tile index with no URL-looking column
    idx = tmp_path / "idx.geojson"
    gpd.GeoDataFrame({"name": ["x"]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326").to_file(idx)
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["fetch_pointcloud"](bbox=[0, 0, 1, 1], tile_index_url=str(idx))
    assert r["ok"] is False and "download-URL column" in r["error"]
