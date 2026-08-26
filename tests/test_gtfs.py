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


def _write_thin_service_feed(path) -> None:
    """A feed whose three stops each break the headway computation differently:

    s3  08:00, 08:10, 08:10, 08:20 — a real 10-minute service, one doubled departure
    s1  12:52, 12:52             — two lines at the same minute, twice a day
    s2  09:00, 09:03             — two departures three minutes apart, nothing else
    """
    files = dict(_GTFS_FILES)
    files["trips.txt"] = (
        "route_id,service_id,trip_id\n"
        "r1,wk,t1\nr1,wk,t2\nr1,wk,t3\nr1,wk,t4\n")
    files["stop_times.txt"] = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,s3,1\n"
        "t1,12:52:00,12:52:00,s1,2\n"
        "t2,08:10:00,08:10:00,s3,1\n"
        "t2,12:52:00,12:52:00,s1,2\n"
        "t3,08:10:00,08:10:00,s3,1\n"
        "t3,09:00:00,09:00:00,s2,2\n"
        "t4,08:20:00,08:20:00,s3,1\n"
        "t4,09:03:00,09:03:00,s2,2\n")
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def test_headway_never_rewards_thin_service(tmp_path):
    """Regression: a stop must not score a fast headway for service it does not have.

    Found in a benchmark run — 'Harting Herbert-Quandt-Allee' was served twice a day,
    both trips at 12:52, and the raw gap between them (0 minutes) sailed through a
    `mean_headway <= 15` filter as the most frequent stop in Regensburg. Two departures
    at the same minute are one chance to board, and a single gap is not a mean.
    """
    import geopandas as gpd
    import pandas as pd

    cache = tmp_path / "cache"
    cache.mkdir()
    _write_thin_service_feed(cache / "de_fv.zip")

    out = tmp_path / "stops.gpkg"
    r = gtfs.fetch_gtfs_stops("de_fv", str(out), str(cache))
    assert r["ok"], r
    gdf = gpd.read_file(out).set_index("stop_id")

    # the real service keeps its headway — de-duplicating must not depress the mean
    # towards 0 either (the raw gaps 0/10/10 would average 6.67)
    assert gdf.loc["s3", "mean_headway"] == pytest.approx(10.0)

    # both thin stops are served, but no headway can be stated for either
    assert int(gdf.loc["s1", "num_trips"]) == 2
    assert int(gdf.loc["s2", "num_trips"]) == 2
    assert pd.isna(gdf.loc["s1", "mean_headway"])  # would have been 0.0
    assert pd.isna(gdf.loc["s2", "mean_headway"])  # would have been 3.0

    # nothing anywhere may claim a 0-minute headway
    assert not (gdf["mean_headway"].fillna(1) == 0).any()


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


# ── what a GTFS "stop" actually is ───────────────────────────────────────────


def _stops_frame(rows):
    import geopandas as gpd
    from shapely.geometry import Point

    return gpd.GeoDataFrame(rows, geometry=[Point(12.1, 49.0)] * len(rows["stop_id"]),
                            crs="EPSG:4326")


def test_stop_composition_splits_stations_from_platforms():
    """A GTFS row is a platform as often as it is a stop.

    Measured 2026-08-23 (`count-bus-stops-in-district`): the 98 rows inside
    Regensburg's Innenstadt were 22 stations plus 76 of their platforms — one
    interchange contributed 32 rows by itself — and the agent reported "98
    Bushaltestellen". Which number answers "how many stops" is the asker's call, so
    the connector reports the split instead of picking a side.
    """
    frame = _stops_frame({
        "stop_id": ["A", "A:1", "A:2", "B"],
        "location_type": [1, 0, 0, 1],
        "parent_station": [None, "A", "A", None],
    })
    c = gtfs._stop_composition(frame)
    assert c["stations"] == 2
    assert c["platforms_and_stops"] == 2
    assert c["rows_with_parent_station"] == 2
    assert "4 rows = 2 station(s) + 2 platform row(s)" in c["counting_hint"]
    assert "say which you counted" in c["counting_hint"]


def test_stop_composition_stays_quiet_without_platforms():
    """No parent stations → nothing to disambiguate, so no hint (warnings that fire
    on clean data are the ones that teach a model to ignore the field)."""
    frame = _stops_frame({
        "stop_id": ["A", "B"],
        "location_type": [0, 0],
        "parent_station": [None, None],
    })
    c = gtfs._stop_composition(frame)
    assert "counting_hint" not in c
    assert c["stations"] == 0 and c["platforms_and_stops"] == 2


def test_stop_composition_survives_a_trimmed_feed():
    """An older feed without those columns must not break the fetch."""
    assert gtfs._stop_composition(_stops_frame({"stop_id": ["A"]})) == {}


# ── a date outside the calendar must not pass for a result ───────────────────


def test_a_date_outside_the_calendar_says_so(tmp_path):
    """`ok: true` with zero service is the worst kind of success.

    Measured 2026-08-23 (`gtfs-stops-departures-map-regensburg`): the agent asked for
    `date="2025-08-25"` — the right weekday, the wrong **year** — and got 1524 stops
    with `stops_with_service: 0`. `num_trips` was then an all-empty column, which a
    GeoPackage stores as TEXT, so the choropleth could not colour by it either. The
    agent spent 30 tool calls fighting the string column; the cause sat three calls
    back as a bare number among other numbers.
    """
    zip_path = tmp_path / "feed.zip"
    _write_synthetic_feed(zip_path)  # calendar covers 2026-06-01..2026-06-30
    r = gtfs.fetch_gtfs_stops(str(zip_path), str(tmp_path / "stops.gpkg"), str(tmp_path),
                              date="2025-08-25")
    assert r["ok"] and r["stops_with_service"] == 0
    warning = r.get("warning") or ""
    assert "NO SERVICE" in warning
    assert "check the year" in warning
    assert "20260601" in warning and "20260630" in warning, "der gültige Bereich gehört dazu"
    assert "Omit `date`" in warning, "die Abhilfe gehört in dieselbe Meldung"


def test_a_date_inside_the_calendar_draws_no_warning(tmp_path):
    zip_path = tmp_path / "feed.zip"
    _write_synthetic_feed(zip_path)
    r = gtfs.fetch_gtfs_stops(str(zip_path), str(tmp_path / "stops.gpkg"), str(tmp_path),
                              date="2026-06-02")  # a Tuesday inside the calendar
    assert r["ok"] and r["stops_with_service"] > 0
    assert "warning" not in r


def _write_exploded_routes_feed(path) -> None:
    """The geOps shape: one route_id per trip, the line living in route_short_name.

    Two trips of line 10 and one of line 12 all serve s1, spread over the headway
    window. By route_id that stop has three "lines"; it has two.
    """
    files = dict(_GTFS_FILES)
    files["routes.txt"] = (
        "route_id,agency_id,route_short_name,route_long_name,route_type\n"
        "r-1:a,A,10,Line Ten,3\n"
        "r-1:b,A,10,Line Ten,3\n"      # same line, second route_id
        "r-2:a,A,12,Line Twelve,3\n")
    files["trips.txt"] = (
        "route_id,service_id,trip_id\n"
        "r-1:a,wk,t1\nr-1:b,wk,t2\nr-2:a,wk,t3\n")
    # Two stops per trip: a route polyline needs at least two points, so a one-stop
    # trip yields no geometry at all and `fetch_gtfs_routes` returns "no routes".
    files["stop_times.txt"] = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,s1,1\n"
        "t1,08:10:00,08:10:00,s2,2\n"
        "t2,08:20:00,08:20:00,s1,1\n"
        "t2,08:30:00,08:30:00,s2,2\n"
        "t3,08:40:00,08:40:00,s1,1\n"
        "t3,08:50:00,08:50:00,s3,2\n")
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def test_num_routes_counts_lines_not_departures(tmp_path):
    """Regression: `num_routes` must not silently become a second trip count.

    Found in `gtfs-ch-transit-map-bern` (2026-08-26). The Swiss geOps feed emits
    one route per trip — 882 301 route_ids for 2 046 678 trips — so counting
    distinct route_ids reported Bern Matte, a single funicular, as 1742 trips on
    "1742 lines". 99 % of served Bern stops had num_trips == num_routes; on the
    German feed it was 2 %, which is why the column looked sound for months.
    """
    import geopandas as gpd

    cache = tmp_path / "cache"
    cache.mkdir()
    _write_exploded_routes_feed(cache / "de_fv.zip")

    out = tmp_path / "stops.gpkg"
    r = gtfs.fetch_gtfs_stops("de_fv", str(out), str(cache))
    assert r["ok"], r
    gdf = gpd.read_file(out).set_index("stop_id")

    assert int(gdf.loc["s1", "num_trips"]) == 3     # three departures…
    assert int(gdf.loc["s1", "num_routes"]) == 2    # …on two lines, not three


def test_line_key_falls_back_to_route_id_without_short_names(tmp_path):
    """A feed with no route_short_name must behave exactly as before the change."""
    import gtfs_kit as gk

    cache = tmp_path / "cache"
    cache.mkdir()
    files = dict(_GTFS_FILES)
    files["routes.txt"] = (
        "route_id,agency_id,route_long_name,route_type\n"
        "r1,A,Line One,3\nr2,A,Line Two,3\n")
    files["trips.txt"] = "route_id,service_id,trip_id\nr1,wk,t1\nr2,wk,t2\n"
    files["stop_times.txt"] = (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "t1,08:00:00,08:00:00,s1,1\nt2,09:00:00,09:00:00,s1,1\n")
    zip_path = cache / "plain.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)

    feed = gk.read_feed(str(zip_path), dist_units="km")
    keys = gtfs._trip_line_key(feed)
    assert set(keys) == {"r1", "r2"}  # unchanged identity, no agency prefix


def test_route_lines_are_grouped_per_line_not_per_route_id(tmp_path):
    """Regression: one polyline per *line*, not one per departure.

    Same cause as `test_num_routes_counts_lines_not_departures`, other tool. Grouping
    `fetch_gtfs_routes` by route_id turned the Bern bbox into **9 428** near-identical
    route lines; by line it is **82**. The German feed was 72 either way, which is
    exactly why the defect stayed invisible until a second country used it.
    """
    import geopandas as gpd

    cache = tmp_path / "cache"
    cache.mkdir()
    _write_exploded_routes_feed(cache / "de_fv.zip")

    out = tmp_path / "routes.gpkg"
    r = gtfs.fetch_gtfs_routes("de_fv", str(out), str(cache))
    assert r["ok"], r
    gdf = gpd.read_file(out)

    # three trips under three route_ids, but only two lines (10 and 12)
    assert r["routes"] == 2, f"expected 2 lines, got {r['routes']}"
    assert set(gdf["route_short_name"]) == {"10", "12"}
    # …and line 10's two trips are aggregated into its one row
    ten = gdf[gdf["route_short_name"] == "10"].iloc[0]
    assert int(ten["num_trips"]) == 2
    # `route_id` stays a real feed id so a join back into the feed lands somewhere
    assert ten["route_id"] in ("r-1:a", "r-1:b")
