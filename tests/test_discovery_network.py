"""Network-dependent discovery tests (osmnx / STAC). Opt in with --run-network."""

import pytest

from _util import tools_of

from chester.capabilities.discovery import DataDiscoveryCapability

pytestmark = pytest.mark.network


def test_geocode_returns_bbox(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["geocode"](query="Ahrweiler, Germany")
    assert r["ok"] and r["bbox"] and len(r["bbox"]) == 4
    west, south, east, north = r["bbox"]
    assert west < east and south < north


def test_osm_features_downloads_buildings(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["osm_features"](
        tags={"building": True},
        bbox=[7.0980, 50.7330, 7.1010, 50.7350],  # small central Bonn bbox
        output_path="osm.geojson",
    )
    assert r["ok"] and r["features"] > 0


def test_stac_search_finds_sentinel2(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["stac_search"](
        bbox=[7.09, 50.53, 7.11, 50.55],
        datetime="2021-07-22/2021-07-24",
        max_cloud=20,
        limit=3,
    )
    assert r["ok"] and r["count"] >= 1
    assert r["items"][0]["assets"]  # band hrefs present


def test_fetch_dem_downloads_elevation(tmp_path):
    from chester import provenance

    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["fetch_dem"](bbox=[7.10, 50.73, 7.12, 50.75], output_path="dem.tif")
    assert r["ok"] and r["tiles_used"] >= 1
    assert r["crs"] == "EPSG:4326" and r["size"][0] > 0 and r["size"][1] > 0

    import rasterio

    with rasterio.open(r["output"]) as ds:
        assert ds.count == 1
        elev = ds.read(1)
    assert float(elev.min()) > 0  # Bonn is well above sea level

    meta = provenance.read_meta(r["output"])
    assert meta["source"] == "connector/copernicus-dem" and "licence" in meta


def test_stac_search_planetary_computer(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["stac_search"](
        bbox=[7.09, 50.53, 7.11, 50.55],
        datetime="2021-07-20/2021-07-26",
        collections=["sentinel-2-l2a"],
        max_cloud=30,
        limit=2,
        catalog="planetary-computer",
    )
    assert r["ok"] and r["catalog"] == "planetary-computer" and r["count"] >= 1
    hrefs = list(r["items"][0]["assets"].values())
    assert any("blob.core.windows.net" in h for h in hrefs)  # needs signing on fetch


def test_fetch_raster_signs_planetary_computer_asset(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    s = tools["stac_search"](
        bbox=[7.09, 50.53, 7.11, 50.55],
        datetime="2021-07-20/2021-07-26",
        collections=["sentinel-2-l2a"],
        max_cloud=30, limit=1, catalog="planetary-computer",
    )
    assets = s["items"][0]["assets"]
    href = assets.get("nir") or next(iter(assets.values()))
    # An unsigned PC blob URL 403s; success proves fetch_raster signed it.
    r = tools["fetch_raster"](url=href, bbox=[7.09, 50.53, 7.11, 50.55], output_path="pc.tif")
    assert r["ok"] and r["size"][0] > 0


def test_wfs_features_mapserver_demo(tmp_path):
    from chester import provenance

    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    # The demo only serves GML for this layer — exercises the GML fallback path.
    r = tools["wfs_features"](
        url="https://demo.mapserver.org/cgi-bin/wfs",
        typename="ms:cities", output_path="cities.geojson", max_features=5,
    )
    assert r["ok"] and r["features"] > 0
    assert provenance.read_meta(r["output"])["source"] == "connector/wfs"


def test_stac_catalogs_discovers_by_keyword(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["stac_catalogs"](keyword="sentinel", limit=5)
    assert r["ok"] and r["count"] >= 1
    assert all("url" in c for c in r["catalogs"])


def test_pointcloud_search_finds_datasets(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    # San Francisco Bay area — dense LiDAR coverage in OpenTopography's catalog.
    r = tools["pointcloud_search"](bbox=[-122.30, 37.85, -122.25, 37.90], limit=5)
    assert r["ok"] and r["count"] >= 1
    assert r["datasets"][0]["name"]


# ── geodata-search feature (open-data catalog → WFS) ─────────────────────────

# The City of Regensburg's Stadtbezirk WFS (the boundary OSM lacks).
_RGBG_WFS_URL = (
    "https://mapservice.regensburg.de/cgi-bin/mapserv"
    "?map=/data/ows/maps/kleingliederung_wfs.map"
)


def test_osm_query_raw_around_point(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    # Raw Overpass QL: cafes within 400 m of the Regensburg cathedral.
    ql = (
        "[out:json][timeout:60];"
        "node(around:400,49.0195,12.0974)[amenity=cafe];out geom;"
    )
    r = tools["osm_query_raw"](overpass_ql=ql, output_path="cafes.geojson")
    assert r["ok"] and r["features"] > 0
    assert r["geometry_types"] == ["Point"]


def test_wfs_capabilities_lists_typenames(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["wfs_capabilities"](url=_RGBG_WFS_URL)
    assert r["ok"] and r["count"] >= 1
    names = [t["name"] for t in r["typenames"]]
    assert any("kleingliederung" in n for n in names)


def test_wms_capabilities_and_fetch_map(tmp_path):
    import rasterio

    from chester import provenance

    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    url = "https://ows.terrestris.de/osm/service"
    caps = tools["wms_capabilities"](url=url)
    assert caps["ok"] and any(l["name"] == "OSM-WMS" for l in caps["layers"])

    r = tools["fetch_wms_map"](
        url=url, layer="OSM-WMS", bbox=[12.08, 49.01, 12.11, 49.03],
        output_path="wms.tif", width=256,
    )
    assert r["ok"] and r["crs"] == "EPSG:4326"
    with rasterio.open(r["output"]) as src:
        # georeferencing must reproduce the requested bbox exactly
        assert abs(src.bounds.left - 12.08) < 1e-6
        assert abs(src.bounds.top - 49.03) < 1e-6
        assert src.count >= 3  # rendered image → RGB(A)
    assert provenance.read_meta(r["output"])["source"] == "connector/wms"


def test_fetch_vector_downloads_geojson(tmp_path):
    from chester import provenance

    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    url = _RGBG_WFS_URL + (
        "&SERVICE=WFS&VERSION=1.1.0&REQUEST=GetFeature"
        "&TYPENAME=ms:kleingliederung&outputformat=geojson"
    )
    r = tools["fetch_vector"](url=url, output_path="districts.geojson")
    assert r["ok"] and r["features"] == 18  # Regensburg has 18 Stadtbezirke
    assert provenance.read_meta(r["output"])["source"] == "connector/download"


def test_geodata_search_finds_regensburg_districts(tmp_path):
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    # govdata.de is deterministic here (a single matching dataset).
    r = tools["geodata_search"](
        query="Stadtbezirke Regensburg", catalog_url="govdata.de", limit=5
    )
    assert r["ok"] and r["count"] >= 1
    top = r["candidates"][0]
    # The mislabelled "CSV" resource is classified as a WFS with a typename.
    wfs = [res for res in top["resources"] if res["service"] == "WFS"]
    assert wfs and any(res["typename"] for res in wfs)


def test_geodata_search_to_wfs_chain_yields_innenstadt(tmp_path):
    """End-to-end acceptance: discover → fetch → the boundary OSM lacks."""
    import geopandas as gpd

    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    search = tools["geodata_search"](
        query="Stadtbezirke Regensburg", catalog_url="govdata.de", limit=5
    )
    wfs_res = next(
        res for res in search["candidates"][0]["resources"] if res["service"] == "WFS"
    )
    fetched = tools["wfs_features"](
        wfs_res["wfs_url"], wfs_res["typename"], "districts.gpkg"
    )
    assert fetched["ok"] and fetched["features"] == 18
    gdf = gpd.read_file(fetched["output"])
    assert "Innenstadt" in set(gdf["Name"].astype(str))
