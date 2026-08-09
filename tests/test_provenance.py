"""Provenance sidecar tests (offline; no QGIS, no network)."""

from __future__ import annotations

from pathlib import Path

from _util import tools_of, write_point

from chester import provenance
from chester.capabilities.mapoutput import MapOutputCapability
from chester.capabilities.vector import VectorCapability
from chester.geocache import GeoCache


def test_write_read_round_trip(tmp_path):
    f = tmp_path / "x.geojson"
    f.write_text("{}")  # the data file need not be valid for the sidecar
    provenance.write_meta(
        str(f), source="connector/osm", tool="osm_features",
        query={"building": True}, crs="EPSG:4326", licence="ODbL", ttl_days=14,
    )
    assert Path(str(f) + ".meta.json").exists()
    meta = provenance.read_meta(str(f))
    assert meta["source"] == "connector/osm"
    assert meta["tool"] == "osm_features"
    assert meta["query"] == '{"building": true}'  # dict stringified, sorted
    assert meta["licence"] == "ODbL" and meta["ttl_days"] == 14
    assert "created_at" in meta


def test_read_meta_absent_is_none(tmp_path):
    assert provenance.read_meta(str(tmp_path / "nope.tif")) is None


def test_geocache_uses_sidecar_source_query_and_ttl(tmp_path):
    p = write_point(tmp_path / "dl.geojson", 7.0, 50.0, "EPSG:4326")
    provenance.write_meta(
        str(p), source="connector/osm", tool="osm_features",
        query={"highway": True}, licence="ODbL", ttl_days=7,
    )
    row = next(r for r in GeoCache(workspace=str(tmp_path)).list() if r["dataset"] == "dl.geojson")
    assert row["source"] == "connector/osm"
    assert row["query"] == '{"highway": true}'
    assert row["licence"] == "ODbL"
    assert row["ttl_days"] == 7  # sidecar TTL wins over the default on first sync


def test_manual_pin_overrides_sidecar_ttl(tmp_path):
    p = write_point(tmp_path / "dl.geojson", 7.0, 50.0, "EPSG:4326")
    provenance.write_meta(str(p), source="connector/osm", tool="osm_features", ttl_days=7)
    cache = GeoCache(workspace=str(tmp_path))
    cache.sync()
    cache.note("dl.geojson", "keep this", ttl_days=365)  # manual pin
    cache.sync()
    row = next(r for r in cache.list() if r["dataset"] == "dl.geojson")
    assert row["ttl_days"] == 365  # remembered pin wins over the sidecar's 7


def test_vector_filter_writes_chester_sidecar(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box

    src = tmp_path / "b.geojson"
    gpd.GeoDataFrame(
        {"h": [8, 18, 25]}, geometry=[box(0, 0, 1, 1), box(2, 2, 3, 3), box(4, 4, 5, 5)],
        crs="EPSG:25832",
    ).to_file(src)
    tools = tools_of(VectorCapability(workspace=str(tmp_path)))
    r = tools["vector_filter"](path=str(src), expression="h > 15", output_path="tall.geojson")
    meta = provenance.read_meta(r["output"])
    assert meta["source"] == "chester" and meta["tool"] == "vector_filter"
    assert meta["query"] == "h > 15"


def test_render_map_embeds_attribution(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box

    p = tmp_path / "osm.geojson"
    gpd.GeoDataFrame({"b": ["yes"]}, geometry=[box(7, 50, 7.1, 50.1)], crs="EPSG:4326").to_file(p)
    provenance.write_meta(
        str(p), source="connector/osm", tool="osm_features",
        licence="© OpenStreetMap contributors (ODbL)",
    )
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    r = tools["render_map"](layers=[str(p)], output_path="map.html")
    assert r["attribution"] == ["© OpenStreetMap contributors (ODbL)"]
    assert "OpenStreetMap contributors" in Path(r["output"]).read_text()


def test_expiry_deletes_sidecar_too(tmp_path):
    p = write_point(tmp_path / "old.geojson", 7.0, 50.0, "EPSG:25832")
    provenance.write_meta(str(p), source="chester", tool="vector_filter")
    sidecar = Path(str(p) + ".meta.json")
    assert sidecar.exists()
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=30)
    cache.sync(today="2026-01-01")
    cache.sync(today="2026-06-01")  # past expiry
    assert not p.exists() and not sidecar.exists()
