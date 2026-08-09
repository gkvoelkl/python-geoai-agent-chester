"""Container-connector tests (offline; GeoPackage file path; PostGIS guarded)."""

from __future__ import annotations

from pathlib import Path

from _util import tools_of

from chester import provenance
from chester.capabilities.connectors import GeoConnectorsCapability


def _multilayer_gpkg(path: Path) -> Path:
    import geopandas as gpd
    from shapely.geometry import Point, box

    gpd.GeoDataFrame(
        {"name": ["a", "b", "c"], "kind": ["res", "com", "res"]},
        geometry=[box(0, 0, 1, 1), box(2, 2, 3, 3), box(4, 4, 5, 5)],
        crs="EPSG:25832",
    ).to_file(path, layer="buildings", driver="GPKG")
    gpd.GeoDataFrame(
        {"n": ["p"]}, geometry=[Point(7, 50)], crs="EPSG:4326"
    ).to_file(path, layer="poi", driver="GPKG")
    return path


def test_geoconnectors_list_discovers_file_containers(tmp_path):
    _multilayer_gpkg(tmp_path / "house.gpkg")
    tools = tools_of(GeoConnectorsCapability(workspace=str(tmp_path), roots=[str(tmp_path)]))
    r = tools["geoconnectors_list"]()
    assert r["ok"]
    names = {c["name"] for c in r["container_connectors"]}
    assert any(n.endswith("house.gpkg") for n in names)
    # PostGIS is unconfigured here, so it must not be advertised.
    assert all(c["kind"] != "container/postgis" for c in r["container_connectors"])
    assert {c["name"] for c in r["query_connectors"]} >= {"geocode", "osm", "stac", "dem"}


def test_geodatasets_list_expands_layers(tmp_path):
    src = _multilayer_gpkg(tmp_path / "house.gpkg")
    tools = tools_of(GeoConnectorsCapability(workspace=str(tmp_path)))
    r = tools["geodatasets_list"](connector=str(src))
    assert r["ok"] and r["count"] == 2
    by_name = {d["dataset"]: d for d in r["datasets"]}
    assert by_name["buildings"]["features"] == 3
    assert by_name["buildings"]["crs"] == "EPSG:25832"
    assert by_name["poi"]["geometry_type"] == "Point"


def test_geodataset_describe_columns(tmp_path):
    src = _multilayer_gpkg(tmp_path / "house.gpkg")
    tools = tools_of(GeoConnectorsCapability(workspace=str(tmp_path)))
    r = tools["geodataset_describe"](connector=str(src), dataset="buildings")
    assert r["ok"] and set(r["columns"]) == {"name", "kind"}
    assert r["features"] == 3


def test_geodataset_fetch_where_subsets_and_sidecars(tmp_path):
    src = _multilayer_gpkg(tmp_path / "house.gpkg")
    tools = tools_of(GeoConnectorsCapability(workspace=str(tmp_path)))
    r = tools["geodataset_fetch"](
        connector=str(src), dataset="buildings", output="sub.gpkg", where={"kind": "res"}
    )
    assert r["ok"] and r["features"] == 2
    assert r["output"].endswith("geocache/sub.gpkg")  # confined to the cache

    meta = provenance.read_meta(r["output"])
    assert meta["source"] == "connector/file" and meta["tool"] == "geodataset_fetch"
    assert '"dataset": "buildings"' in meta["query"]


def test_geodataset_fetch_unknown_where_column_errors(tmp_path):
    src = _multilayer_gpkg(tmp_path / "house.gpkg")
    tools = tools_of(GeoConnectorsCapability(workspace=str(tmp_path)))
    r = tools["geodataset_fetch"](
        connector=str(src), dataset="buildings", output="x.gpkg", where={"nope": "1"}
    )
    assert r["ok"] is False and "nope" in r["error"]


def test_unknown_connector_is_reported(tmp_path):
    tools = tools_of(GeoConnectorsCapability(workspace=str(tmp_path)))
    r = tools["geodatasets_list"](connector="does-not-exist.gpkg")
    assert r["ok"] is False and "no such container" in r["error"]


def test_postgis_inert_when_unconfigured(tmp_path):
    tools = tools_of(GeoConnectorsCapability(workspace=str(tmp_path)))
    r = tools["geodatasets_list"](connector="postgis")
    assert r["ok"] is False and "not configured" in r["error"]


def test_referencing_a_non_container_file_errors(tmp_path):
    p = tmp_path / "plain.geojson"
    p.write_text("{}")
    tools = tools_of(GeoConnectorsCapability(workspace=str(tmp_path)))
    r = tools["geodatasets_list"](connector=str(p))
    assert r["ok"] is False and "not a GeoPackage/SpatiaLite" in r["error"]
