"""GeoCache inventory tests (offline; geopandas/rasterio, no QGIS, no network)."""

from __future__ import annotations

from _util import tools_of, write_building_sample, write_point

from chester import geofacts
from chester.capabilities.inventory import GeoInventoryCapability
from chester.geocache import GeoCache


def _gpkg_two_layers(path):
    import geopandas as gpd
    from shapely.geometry import Point

    a = gpd.GeoDataFrame({"n": ["x"]}, geometry=[Point(7, 50)], crs="EPSG:4326")
    a.to_file(path, layer="poi", driver="GPKG")
    b = gpd.GeoDataFrame({"n": ["y", "z"]}, geometry=[Point(8, 51), Point(9, 52)], crs="EPSG:4326")
    b.to_file(path, layer="more", driver="GPKG")


# ── geofacts ─────────────────────────────────────────────────────────────────


def test_geofacts_vector_meta_and_full_agree(tmp_path):
    sample = write_building_sample(tmp_path)
    meta = geofacts.vector_facts(str(sample["buildings"]))            # fast path
    full = geofacts.vector_facts(str(sample["buildings"]), full=True)  # full read
    assert meta["feature_count"] == full["feature_count"] == 3
    assert meta["crs"] == full["crs"] == "EPSG:25832"
    assert "true_height" in full["columns"]
    assert meta["bounds_wgs84"] and full["bounds_wgs84"]


def test_geofacts_lists_container_layers(tmp_path):
    _gpkg_two_layers(tmp_path / "multi.gpkg")
    assert sorted(geofacts.list_layers(str(tmp_path / "multi.gpkg"))) == ["more", "poi"]


# ── sync: add / refresh / drop ───────────────────────────────────────────────


def test_sync_adds_and_lists(tmp_path):
    write_building_sample(tmp_path)  # buildings.geojson + dtm.tif + dsm.tif
    cache = GeoCache(workspace=str(tmp_path))
    summary = cache.sync()
    assert summary["total"] == 3  # one vector + two rasters
    names = {r["dataset"] for r in cache.list()}
    assert "buildings.geojson" in names and "dtm.tif" in names


def test_sync_drops_missing(tmp_path):
    sample = write_building_sample(tmp_path)
    cache = GeoCache(workspace=str(tmp_path))
    cache.sync()
    sample["buildings"].unlink()
    summary = cache.sync()
    assert "buildings.geojson" in summary["dropped"]
    assert "buildings.geojson" not in {r["dataset"] for r in cache.list()}


def test_sync_refreshes_changed_and_keeps_created(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box

    p = tmp_path / "f.geojson"
    gpd.GeoDataFrame({"h": [1]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:25832").to_file(p)
    cache = GeoCache(workspace=str(tmp_path))
    cache.sync(today="2026-01-01")
    created = next(r for r in cache.list(today="2026-01-01") if r["dataset"] == "f.geojson")["created"]

    # grow the layer; a later sync must reflect the new count but keep created_at
    gpd.GeoDataFrame(
        {"h": [1, 2, 3]}, geometry=[box(0, 0, 1, 1), box(2, 2, 3, 3), box(4, 4, 5, 5)],
        crs="EPSG:25832",
    ).to_file(p)
    summary = cache.sync(today="2026-01-05")
    assert "f.geojson" in summary["refreshed"]
    row = next(r for r in cache.list(today="2026-01-05") if r["dataset"] == "f.geojson")
    assert row["features"] == 3
    assert row["created"] == created  # creation date is remembered, not reset


# ── multi-layer containers ───────────────────────────────────────────────────


def test_multilayer_container_expands(tmp_path):
    _gpkg_two_layers(tmp_path / "multi.gpkg")
    cache = GeoCache(workspace=str(tmp_path))
    cache.sync()
    keys = {r["dataset"] for r in cache.list()}
    assert "multi.gpkg::poi" in keys and "multi.gpkg::more" in keys


# ── expiry ───────────────────────────────────────────────────────────────────


def test_expired_dataset_is_deleted(tmp_path):
    p = write_point(tmp_path / "old.geojson", 7.0, 50.0, "EPSG:25832")
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=30)
    cache.sync(today="2026-01-01")
    assert p.exists()
    summary = cache.sync(today="2026-06-01")  # well past 30 days
    assert "old.geojson" in summary["expired"]
    assert not p.exists()  # the file itself is removed, not just the row


def test_source_user_never_expires(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    p = write_point(data_root / "ref.geojson", 7.0, 50.0, "EPSG:25832")
    cache = GeoCache(workspace=str(tmp_path / "ws"), roots=[str(data_root)], default_ttl_days=1)
    cache.sync(today="2026-01-01")
    summary = cache.sync(today="2030-01-01")  # far future
    assert summary["expired"] == []
    assert p.exists()
    row = next(r for r in cache.list(today="2030-01-01") if r["dataset"].endswith("ref.geojson"))
    assert row["source"] == "user" and row["expires"] == "never"


def test_touch_keeps_dataset_from_expiring(tmp_path):
    write_point(tmp_path / "keep.geojson", 7.0, 50.0, "EPSG:25832")
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=30)
    cache.sync(today="2026-01-01")
    cache.touch("keep.geojson", today="2026-05-01")  # used again, well after creation
    summary = cache.sync(today="2026-05-15")  # >30d since creation, but only 14d since use
    assert summary["expired"] == []
    assert (tmp_path / "keep.geojson").exists()


def test_resolve_path_touches_cached_input(tmp_path):
    from datetime import date

    from chester.workspace import resolve_path

    # a file living in the confined cache dir
    geo = tmp_path / "geocache"
    geo.mkdir()
    write_point(geo / "x.geojson", 7.0, 50.0, "EPSG:25832")
    cache = GeoCache(workspace=str(tmp_path))
    cache.sync(today="2020-01-01")  # stamp an old last_used

    # resolving it as an input (the file exists) must touch it via the read path
    resolved = resolve_path("x.geojson", str(tmp_path))
    assert resolved.endswith("geocache/x.geojson")
    row = next(r for r in cache.list() if r["dataset"] == "geocache/x.geojson")
    assert row["last_used"] == date.today().isoformat()


def test_resolve_path_does_not_touch_new_output(tmp_path):
    from chester.workspace import resolve_path

    cache = GeoCache(workspace=str(tmp_path))
    cache.sync()
    # resolving a not-yet-existing output name is a write, not a read: it must not
    # appear in the inventory until it is actually created and synced.
    out = resolve_path("brand_new_output.geojson", str(tmp_path))
    assert out.endswith("geocache/brand_new_output.geojson")
    assert "geocache/brand_new_output.geojson" not in {r["dataset"] for r in cache.list()}


def test_multilayer_only_deleted_when_all_layers_expire(tmp_path):
    _gpkg_two_layers(tmp_path / "multi.gpkg")
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=30)
    cache.sync(today="2026-01-01")
    # touch one layer so it is not expired; the container must survive
    cache.touch("multi.gpkg", layer="poi", today="2026-06-01")
    summary = cache.sync(today="2026-06-02")
    assert summary["expired"] == []
    assert (tmp_path / "multi.gpkg").exists()


# ── notes / pinning ──────────────────────────────────────────────────────────


def test_note_pins_ttl_and_persists_across_sync(tmp_path):
    write_point(tmp_path / "n.geojson", 7.0, 50.0, "EPSG:25832")
    cache = GeoCache(workspace=str(tmp_path))
    cache.sync()
    r = cache.note("n.geojson", "the important one", ttl_days=365)
    assert r["ok"] and r["ttl_days"] == 365
    cache.sync()  # a later reconcile must keep the note and the pinned ttl
    row = next(x for x in cache.list() if x["dataset"] == "n.geojson")
    assert row["note"] == "the important one" and row["ttl_days"] == 365


def test_note_unknown_dataset_reports_known(tmp_path):
    write_point(tmp_path / "a.geojson", 7.0, 50.0, "EPSG:25832")
    cache = GeoCache(workspace=str(tmp_path))
    cache.sync()
    r = cache.note("does-not-exist.geojson", "x")
    assert r["ok"] is False and "a.geojson" in r["known"]


# ── CRS flag ─────────────────────────────────────────────────────────────────


def test_crs_warning_flags_geographic(tmp_path):
    write_point(tmp_path / "geo.geojson", 7.0, 50.0, "EPSG:4326")     # degrees
    write_point(tmp_path / "proj.geojson", 500000, 5600000, "EPSG:25832")  # metric
    cache = GeoCache(workspace=str(tmp_path))
    rows = {r["dataset"]: r for r in cache.list()}
    assert rows["geo.geojson"]["crs_warning"]      # geographic → flagged
    assert rows["proj.geojson"]["crs_warning"] is None


# ── capability surface ───────────────────────────────────────────────────────


def test_capability_exposes_tools(tmp_path):
    write_point(tmp_path / "c.geojson", 500000, 5600000, "EPSG:25832")
    tools = tools_of(GeoInventoryCapability(workspace=str(tmp_path)))
    assert set(tools) == {"geocache_list", "geocache_sync", "geocache_note"}
    r = tools["geocache_list"]()
    assert r["ok"] and r["count"] == 1 and r["datasets"][0]["dataset"] == "c.geojson"
