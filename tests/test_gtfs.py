"""Tests for the GTFS transit connector (chester/gtfs.py).

The feed registry, the unknown-feed guard and the full stops+service-stats pipeline
are covered offline against a tiny synthetic GTFS feed (placed in the cache so no
download happens); one opt-in network test hits the small real de_fv feed.
"""

from __future__ import annotations

import zipfile

import pytest

from chester import gtfs

_GTFS_FILES = {
    "agency.txt": (
        "agency_id,agency_name,agency_url,agency_timezone\n"
        "A,Test,https://example.org,Europe/Berlin\n"),
    "stops.txt": (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "s1,Alpha,49.010,12.090\n"
        "s2,Beta,49.020,12.100\n"
        "s3,Gamma,49.030,12.110\n"),
    "routes.txt": (
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "r1,A,1,Line One,3\n"),
    "trips.txt": (
        "route_id,service_id,trip_id\n"
        "r1,wk,t1\n"
        "r1,wk,t2\n"),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,s1,1\n"
        "t1,08:10:00,08:10:00,s2,2\n"
        "t1,08:20:00,08:20:00,s3,3\n"
        "t2,09:00:00,09:00:00,s1,1\n"
        "t2,09:10:00,09:10:00,s2,2\n"
        "t2,09:20:00,09:20:00,s3,3\n"),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
        "start_date,end_date\n"
        "wk,1,1,1,1,1,0,0,20260601,20260630\n"),
}


def _write_synthetic_feed(path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in _GTFS_FILES.items():
            zf.writestr(name, content)


def _write_calendar_dates_feed(path) -> None:
    """Like the synthetic feed but service is defined ONLY via calendar_dates.txt
    (no weekday flags) — the Swiss-feed structure that broke compute_stop_stats."""
    files = dict(_GTFS_FILES)
    del files["calendar.txt"]
    files["calendar_dates.txt"] = (
        "service_id,date,exception_type\n"
        "wk,20260602,1\n")  # service 'wk' runs only on 2026-06-02
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def test_feeds_catalog_lists_dach_feeds():
    cat = {f["feed"]: f for f in gtfs.feeds_catalog()}
    assert {"de_fv", "de_rv", "de_nv", "de_full"} <= set(cat)       # Germany
    assert {"ch_rail", "ch_bus", "ch_full"} <= set(cat)             # Switzerland
    assert {"at_full", "at_oebb"} <= set(cat)                       # Austria (gated)
    assert cat["de_nv"]["country"] == "DE" and cat["ch_full"]["country"] == "CH"
    assert cat["de_nv"]["credential_free"] and not cat["at_oebb"]["credential_free"]


def test_fetch_gtfs_stops_unknown_feed(tmp_path):
    r = gtfs.fetch_gtfs_stops("de_xx", str(tmp_path / "x.gpkg"), str(tmp_path))
    assert r["ok"] is False and "unknown feed" in r["error"]


def test_fetch_gtfs_stops_gated_feed_refuses_with_portal(tmp_path):
    r = gtfs.fetch_gtfs_stops("at_oebb", str(tmp_path / "x.gpkg"), str(tmp_path))
    assert r["ok"] is False and r.get("gated") is True
    assert "terms-of-use" in r["error"] and r["portal"].startswith("http")


def test_fetch_gtfs_stops_accepts_local_zip_path(tmp_path):
    """A local GTFS zip path works as `feed` (the gated/foreign-feed escape hatch)."""
    import geopandas as gpd

    zip_path = tmp_path / "my_feed.zip"
    _write_synthetic_feed(zip_path)
    out = tmp_path / "stops.gpkg"
    r = gtfs.fetch_gtfs_stops(str(zip_path), str(out), str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["feed"] == "my_feed.zip" and r["stops"] == 3
    assert gpd.read_file(out).crs.to_epsg() == 4326


def test_fetch_gtfs_stops_tool_wired_in_capability():
    from agent_build import _capability_tools
    from chester.capabilities import GeoTransitCapability

    tools = _capability_tools(GeoTransitCapability(workspace="/tmp/ws"))
    assert {"gtfs_feeds", "fetch_gtfs_stops", "fetch_gtfs_routes"} <= set(tools)


def test_fetch_gtfs_routes_synthetic_feed(tmp_path):
    """Route line + per-route stats from the synthetic feed (one route, two trips)."""
    import geopandas as gpd

    cache = tmp_path / "cache"
    cache.mkdir()
    _write_synthetic_feed(cache / "de_fv.zip")

    out = tmp_path / "routes.gpkg"
    r = gtfs.fetch_gtfs_routes("de_fv", str(out), str(cache))
    assert r["ok"], r
    assert r["routes"] == 1 and r["crs"] == "EPSG:4326"
    gdf = gpd.read_file(out)
    assert gdf.crs.to_epsg() == 4326
    assert gdf.geometry.iloc[0].geom_type == "LineString"
    row = gdf.iloc[0]
    assert int(row["num_trips"]) == 2 and int(row["num_stops"]) == 3


def test_fetch_gtfs_stops_synthetic_feed(tmp_path):
    """Full pipeline offline: a cached synthetic feed → stops + service stats."""
    import geopandas as gpd

    cache = tmp_path / "cache"
    cache.mkdir()
    _write_synthetic_feed(cache / "de_fv.zip")  # pre-cache so no download happens

    out = tmp_path / "stops.gpkg"
    r = gtfs.fetch_gtfs_stops("de_fv", str(out), str(cache))
    assert r["ok"], r
    assert r["crs"] == "EPSG:4326" and r["stops"] == 3
    assert r["stops_with_service"] == 3  # all three stops served by the two trips

    gdf = gpd.read_file(out)
    assert gdf.crs.to_epsg() == 4326
    assert "num_trips" in gdf.columns and "mean_headway" in gdf.columns
    # each stop is served by exactly the two trips of the day
    assert set(gdf["num_trips"].astype(int)) == {2}


def test_service_stats_honour_calendar_dates(tmp_path):
    """Regression: service defined only via calendar_dates.txt must still be counted
    (gtfs-kit's compute_stop_stats misses it; the connector computes from get_trips)."""
    import geopandas as gpd

    cache = tmp_path / "cache"
    cache.mkdir()
    _write_calendar_dates_feed(cache / "de_fv.zip")

    out = tmp_path / "stops.gpkg"
    # the service runs on 2026-06-02 — the stops must show the two trips that day
    r = gtfs.fetch_gtfs_stops("de_fv", str(out), str(cache), date="2026-06-02")
    assert r["ok"], r
    assert r["stops_with_service"] == 3
    gdf = gpd.read_file(out)
    assert set(gdf["num_trips"].astype(int)) == {2}


def test_fetch_gtfs_stops_bbox_windows_the_feed(tmp_path):
    import geopandas as gpd

    cache = tmp_path / "cache"
    cache.mkdir()
    _write_synthetic_feed(cache / "de_fv.zip")

    out = tmp_path / "stops_bbox.gpkg"
    # a bbox that only covers stop s1 (49.010, 12.090)
    r = gtfs.fetch_gtfs_stops("de_fv", str(out), str(cache),
                              bbox_wgs84=[12.085, 49.005, 12.095, 49.015])
    assert r["ok"], r
    gdf = gpd.read_file(out)
    assert set(gdf["stop_name"]) == {"Alpha"}


@pytest.mark.network
def test_fetch_gtfs_stops_de_fv_live(tmp_path):
    import geopandas as gpd

    out = tmp_path / "fv.gpkg"
    r = gtfs.fetch_gtfs_stops("de_fv", str(out), str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["stops"] > 100 and r["crs"] == "EPSG:4326"
    gdf = gpd.read_file(out)
    assert "num_trips" in gdf.columns and len(gdf) > 100


@pytest.mark.network
def test_fetch_gtfs_routes_de_fv_live(tmp_path):
    import geopandas as gpd

    out = tmp_path / "routes.gpkg"
    r = gtfs.fetch_gtfs_routes("de_fv", str(out), str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["routes"] > 10 and r["crs"] == "EPSG:4326"
    gdf = gpd.read_file(out)
    assert "num_trips" in gdf.columns and gdf.geometry.notna().all()
    assert gdf.geom_type.isin(["LineString", "MultiLineString"]).all()


@pytest.mark.network
def test_fetch_gtfs_stops_ch_rail_live(tmp_path):
    """Switzerland via geOps, windowed to a Bern bbox."""
    import geopandas as gpd

    out = tmp_path / "ch.gpkg"
    r = gtfs.fetch_gtfs_stops("ch_rail", str(out), str(tmp_path / "cache"),
                              bbox_wgs84=[7.40, 46.93, 7.48, 46.97])
    assert r["ok"], r
    assert r["crs"] == "EPSG:4326" and r["stops"] > 0
    assert "num_trips" in gpd.read_file(out).columns
