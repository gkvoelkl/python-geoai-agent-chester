"""GTFS public-transit connector — DACH open feeds (Phase 6.2 / concept Phase C).

QGIS' network analysis is single-mode and timetable-blind; public transit needs a
timetable-aware source. This connector reaches the **open GTFS feeds** of the
German-speaking region and turns them into geodata Chester can map and measure:
stop points carrying **service-quality** attributes (trips/routes per day, mean
headway, service span) via `gtfs-kit`.

**Credential-free by design** (the recurring Chester constraint — cf. the GENESIS
accounts / BKG securityGate): the registry holds only feeds that download with a plain
anonymous GET.

- 🇩🇪 **Germany** via **gtfs.de** (GTFS generated daily from the DELFI open dataset,
  CC-BY, no registration): ``de_fv`` long-distance rail ~1 MB · ``de_rv`` regional rail
  ~10 MB · ``de_nv`` local transit ~220 MB · ``de_full`` everything ~230 MB.
- 🇨🇭 **Switzerland** via **geOps** (``gtfs.geops.ch``, converted daily from
  opentransportdata.swiss, free direct download): ``ch_rail`` ~18 MB · ``ch_bus``
  ~100 MB · ``ch_full`` everything ~150 MB.
- 🇦🇹 **Austria** is **gated** — both the ÖBB feed and the national
  (Mobilitätsverbünde Österreich) feed sit behind a terms-of-use confirmation, so they
  are **not** credential-free. They are listed (``gated=True``) with their portal so
  the agent reports them honestly instead of guessing a broken URL; fetch them by
  downloading the zip manually and passing its **local path** as ``feed`` (see below).

Any **local GTFS zip path** works as ``feed`` too — so a gated / foreign / private feed
is usable once the file is on disk.

The large feeds are national, so a **bbox is strongly recommended**: the feed is
``restrict_to_area``'d to the bbox *before* the (otherwise nationwide) service-stat
computation, which keeps it tractable. Later: GTFS-Realtime and multimodal
(walk+transit) isochrones extending ``walkability``.

Pure core (no SelmaKit dep, like ``lod2.py`` / ``swisstopo.py``).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

_UA = {"User-Agent": "Mozilla/5.0 (Chester Geo-AI)"}
# distinct departures a stop needs inside the headway window before a mean headway is
# stated at all — two departures give one gap, and one gap is not a mean
_MIN_DEPARTURES = 3
_GTFS_DE = "CC-BY 4.0 — gtfs.de (generated daily from the DELFI open dataset)"
_GEOPS = "CC-BY 4.0 — geOps (gtfs.geops.ch, converted daily from opentransportdata.swiss)"
_AT = "CC-BY 4.0 — Mobilitätsverbünde Österreich / ÖBB"


@dataclass(frozen=True)
class GtfsFeed:
    """One GTFS feed: its download URL, title, licence, country and size hint.

    ``gated`` feeds are **not** credential-free (a terms-of-use confirmation or login
    guards them); ``url`` is then the portal page, and the feed is listed but not
    auto-downloaded — the user fetches it manually and passes the local zip path.
    """

    name: str
    url: str
    title: str
    licence: str
    approx_mb: int
    note: str
    country: str = "DE"
    gated: bool = False


# Only credential-free feeds are auto-downloadable. Names carry a country prefix.
FEEDS: dict[str, GtfsFeed] = {
    # 🇩🇪 gtfs.de (`latest.zip`, regenerated daily, covering the next ~30 days)
    "de_fv": GtfsFeed(
        "de_fv", "https://download.gtfs.de/germany/fv_free/latest.zip",
        "Deutschland Schienenfernverkehr", _GTFS_DE, 1,
        "long-distance rail (ICE/IC/EC) — small, ideal for a quick test"),
    "de_rv": GtfsFeed(
        "de_rv", "https://download.gtfs.de/germany/rv_free/latest.zip",
        "Deutschland Schienenregionalverkehr", _GTFS_DE, 10, "regional rail"),
    "de_nv": GtfsFeed(
        "de_nv", "https://download.gtfs.de/germany/nv_free/latest.zip",
        "Deutschland Nahverkehr", _GTFS_DE, 222,
        "local transit: bus / tram / metro + regional — national, pass a bbox"),
    "de_full": GtfsFeed(
        "de_full", "https://download.gtfs.de/germany/free/latest.zip",
        "Deutschland gesamt", _GTFS_DE, 232, "everything (nv + fv) — national, pass a bbox"),
    # 🇨🇭 geOps (gtfs.geops.ch, subsets by transport mode + a complete feed)
    "ch_rail": GtfsFeed(
        "ch_rail", "https://gtfs.geops.ch/dl/gtfs_train.zip",
        "Schweiz Schienenverkehr", _GEOPS, 18, "rail only — small, good for a test",
        country="CH"),
    "ch_bus": GtfsFeed(
        "ch_bus", "https://gtfs.geops.ch/dl/gtfs_bus.zip",
        "Schweiz Busverkehr", _GEOPS, 103, "bus only — national, pass a bbox",
        country="CH"),
    "ch_full": GtfsFeed(
        "ch_full", "https://gtfs.geops.ch/dl/gtfs_complete.zip",
        "Schweiz gesamt", _GEOPS, 149,
        "everything (rail + bus + tram + boat + …) — national, pass a bbox",
        country="CH"),
    # 🇦🇹 gated — a terms-of-use confirmation guards the download (not credential-free)
    "at_full": GtfsFeed(
        "at_full", "https://data.mobilitaetsverbuende.at/de/data-sets",
        "Österreich gesamt (Mobilitätsverbünde)", _AT, 0,
        "GATED: terms-of-use confirmation required — download manually, then pass the "
        "local zip path as 'feed'", country="AT", gated=True),
    "at_oebb": GtfsFeed(
        "at_oebb", "https://data.oebb.at/de/datensaetze~soll-fahrplan-gtfs~",
        "Österreich ÖBB (Schiene)", _AT, 0,
        "GATED: terms-of-use confirmation required — download manually, then pass the "
        "local zip path as 'feed'", country="AT", gated=True),
}


def feeds_catalog() -> list[dict]:
    """The GTFS feeds: name, country, title, licence, size, credential-free? and note."""
    return [{"feed": f.name, "country": f.country, "title": f.title,
             "licence": f.licence, "approx_mb": f.approx_mb,
             "credential_free": not f.gated,
             "url": f.url, "note": f.note} for f in FEEDS.values()]


def _ensure_feed(name: str, cache_dir: str) -> str:
    """Download the feed zip once into ``cache_dir``; return the cached path."""
    feed = FEEDS[name]
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(cache_dir) / f"{name}.zip"
    if dest.exists() and dest.stat().st_size > 0:
        return str(dest)
    tmp = dest.with_suffix(".zip.part")
    with urlopen(Request(feed.url, headers=_UA), timeout=600) as r, open(tmp, "wb") as fh:
        shutil.copyfileobj(r, fh)
    tmp.replace(dest)  # atomic — a half-download never poisons the cache
    return str(dest)


def _load_feed(raw: str, cache_dir: str):
    """Resolve ``feed`` (registered name / gated / local zip path) and read it.

    Returns ``((feed_obj, name, licence), None)`` on success, or ``(None, error_dict)``.
    Shared by ``fetch_gtfs_stops`` and ``fetch_gtfs_routes`` — one resolution path.
    """
    import os

    import gtfs_kit as gk

    raw = raw.strip()
    spec = FEEDS.get(raw.lower())
    if spec is not None:
        if spec.gated:
            return None, {"ok": False, "error": f"feed '{spec.name}' is not credential-"
                          f"free — it requires a terms-of-use confirmation at {spec.url}."
                          " Download the GTFS zip manually, then pass its local path as "
                          "'feed'.", "portal": spec.url, "licence": spec.licence,
                          "gated": True}
        name, licence = spec.name, spec.licence
        try:
            zip_path = _ensure_feed(name, cache_dir)
        except Exception as exc:  # noqa: BLE001
            return None, {"ok": False, "error": f"download failed: "
                          f"{type(exc).__name__}: {exc}"}
    elif os.path.isfile(raw):
        # a user-supplied local GTFS zip (gated / foreign / private feed on disk)
        name, licence, zip_path = os.path.basename(raw), "user-supplied GTFS", raw
    else:
        return None, {"ok": False, "error": f"unknown feed '{raw}' (not a registered "
                      "feed name and not an existing file path)", "known": sorted(FEEDS)}

    try:
        feed_obj = gk.read_feed(zip_path, dist_units="km")
    except Exception as exc:  # noqa: BLE001
        return None, {"ok": False, "error": f"GTFS parse failed: "
                      f"{type(exc).__name__}: {exc}"}
    return (feed_obj, name, licence), None


def _restrict_to_bbox(feed_obj, bbox_wgs84):
    """Restrict a feed to a WGS84 bbox (keeps whole trips touching the area)."""
    import geopandas as gpd
    from shapely.geometry import box

    area = gpd.GeoDataFrame(geometry=[box(*bbox_wgs84)], crs="EPSG:4326")
    return feed_obj.restrict_to_area(area)


def _pick_date(feed_obj, date: str | None) -> str | None:
    """A representative service date: the caller's, else a weekday of the first week."""
    if date:
        return date.replace("-", "")
    week = feed_obj.get_first_week()
    if not week:
        return None
    return week[1] if len(week) > 1 else week[0]  # index 1 == Tuesday


def _service_window(feed_obj) -> tuple[str, str] | None:
    """First and last date the feed's calendar actually covers, or ``None``.

    Best-effort: a feed without a readable calendar simply yields no window, and
    the caller then reports the defect without the range.
    """
    try:
        dates = feed_obj.get_dates()
    except Exception:  # noqa: BLE001 - a diagnostic must never break the fetch
        return None
    return (dates[0], dates[-1]) if dates else None


def _no_service_warning(feed_obj, svc_date: str | None, total: int) -> str | None:
    """Say it plainly when the chosen date has no service at all.

    ``ok: true`` with ``stops_with_service: 0`` is the worst kind of success: the
    layer exists, every stop is there, and the statistic the caller asked for is
    empty — which also makes `num_trips` an all-null column that lands in the
    GeoPackage as *text*, so a choropleth on it cannot work either. Measured
    2026-08-23 (`gtfs-stops-departures-map-regensburg`): the agent asked for
    ``date="2025-08-25"`` — the right weekday, the wrong **year** — got 1524 stops
    with zero service, and spent 30 tool calls fighting the string column instead of
    the date. The cause was three calls back and mentioned only as a bare number.
    """
    if total <= 0:
        return None
    window = _service_window(feed_obj)
    span = f" This feed's calendar covers {window[0]}–{window[1]}." if window else ""
    return (
        f"NO SERVICE on {svc_date}: 0 of {total} stops have any departure that day, so "
        f"num_trips / num_routes / headways are all empty — and an all-empty column is "
        f"written as TEXT, which no choropleth can colour by. The date is almost "
        f"certainly outside the feed's service calendar (check the year).{span} "
        f"Omit `date` to get a representative weekday from the feed itself, and re-run."
    )


def _hms_to_seconds(series):
    """GTFS ``HH:MM:SS`` (may exceed 24h) → seconds since midnight, vectorised."""
    parts = series.astype(str).str.split(":", expand=True)
    return (parts[0].astype("float") * 3600 + parts[1].astype("float") * 60
            + parts[2].astype("float"))


def _route_line_key(feed_obj):
    """Map route_id → a stable **line** identity. See :func:`_trip_line_key`."""
    import pandas as pd

    routes = feed_obj.routes
    key = routes["route_id"].astype(str)
    if "route_short_name" in routes.columns:
        short = routes["route_short_name"].fillna("").astype(str).str.strip()
        if "agency_id" in routes.columns:
            agency = routes["agency_id"].fillna("").astype(str).str.strip()
            qualified = agency + "|" + short
        else:
            qualified = short
        key = key.where(short == "", qualified)  # empty short name → keep route_id
    lookup = pd.Series(key.to_numpy(), index=routes["route_id"])
    return lookup[~lookup.index.duplicated()]  # a repeated route_id must not fan out


def _trip_line_key(feed_obj):
    """Map trip_id → a stable **line** identity, for counting lines at a stop.

    ``route_id`` is not one. The Swiss geOps feed emits roughly one route per trip
    — 882 301 route_ids for 2 046 678 trips, median 1 trip each — so counting
    distinct route_ids counts *departures* and labels them lines: Bern Matte came
    back as 1742 trips on "1742 lines", and 99 % of served stops had num_trips
    exactly equal to num_routes. The German gtfs.de feed does not do this (19 of
    901), which is why the column looked sound until a second country used it.

    What a passenger calls a line is ``route_short_name``, qualified by agency so
    that bus 1 in two towns stays two lines. Falls back to ``route_id`` wherever no
    short name exists, so a feed without the column behaves exactly as before.
    """
    return feed_obj.trips.set_index("trip_id")["route_id"].map(_route_line_key(feed_obj))


def _stop_service_stats(feed_obj, date: str, headway_start: str, headway_end: str):
    """Per-stop service stats for ``date`` computed from the day's **active trips**.

    Replaces gtfs-kit's ``compute_stop_stats``, which reads the weekday flags in
    ``calendar.txt`` and so badly under-counts feeds that encode service almost
    entirely in ``calendar_dates.txt`` (e.g. the Swiss geOps feed). ``feed.get_trips
    (date)`` honours both, so counting departures over its active trips is correct
    across feed structures. Returns a DataFrame: stop_id, num_trips, num_routes,
    mean/min/max_headway (minutes, within the window), start_time, end_time (HH:MM:SS).
    The headway columns are null where none can be stated — see the rules below.
    """
    import pandas as pd

    active = set(feed_obj.get_trips(date)["trip_id"])
    st = feed_obj.stop_times[feed_obj.stop_times["trip_id"].isin(active)].copy()
    if st.empty:
        return pd.DataFrame(columns=["stop_id", "num_trips", "num_routes",
                                     "mean_headway", "min_headway", "max_headway",
                                     "start_time", "end_time"])
    st["dep"] = _hms_to_seconds(st["departure_time"])
    st["route_id"] = st["trip_id"].map(_trip_line_key(feed_obj))

    grp = st.groupby("stop_id")
    out = grp.agg(num_trips=("trip_id", "count"),
                  num_routes=("route_id", "nunique"),
                  _start=("dep", "min"), _end=("dep", "max")).reset_index()

    def _fmt(sec):
        if pd.isna(sec):  # a stop whose active trips all have empty departure_time
            return None
        sec = int(sec)
        return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"

    out["start_time"] = out["_start"].map(_fmt)
    out["end_time"] = out["_end"].map(_fmt)

    # headways within the window: gaps between consecutive departure *times* per stop.
    # Two rules keep a headway from claiming more service than there is:
    #   1. de-duplicate identical departure times. Two lines leaving at 12:52 are one
    #      opportunity to board, not two — counted raw they yield a 0-minute gap that
    #      sorts ahead of every real service and slips through any `mean_headway <= n`
    #      filter (and depresses the mean at busy stops by up to 33 min).
    #   2. require at least two gaps. With one gap the "mean" is that gap, so two
    #      departures three minutes apart and nothing else all day reads as a 3-minute
    #      service. Below that, null — as for a stop with a single trip, and meaning
    #      the same thing: no headway can be stated. See tests/test_gtfs.py.
    ws = _hms_to_seconds(pd.Series([headway_start])).iloc[0]
    we = _hms_to_seconds(pd.Series([headway_end])).iloc[0]
    win = st[(st["dep"] >= ws) & (st["dep"] <= we)].sort_values(["stop_id", "dep"])
    win = win.drop_duplicates(["stop_id", "dep"])
    gaps = win.groupby("stop_id")["dep"].diff() / 60.0  # minutes
    gaps = gaps.groupby(win["stop_id"])
    hw = gaps.agg(["mean", "min", "max"]).rename(
        columns={"mean": "mean_headway", "min": "min_headway", "max": "max_headway"})
    hw = hw[win.groupby("stop_id")["dep"].size().reindex(hw.index) >= _MIN_DEPARTURES]
    out = out.merge(hw, on="stop_id", how="left")
    return out[["stop_id", "num_trips", "num_routes", "mean_headway",
                "min_headway", "max_headway", "start_time", "end_time"]]


def fetch_gtfs_stops(feed: str, output_path: str, cache_dir: str,  # noqa: C901
# C901-Ausnahme: Feedaufloesung (registriert/gated/lokal) plus bbox-, Datums- und Stop-Zahl-
# Absicherungen
                     bbox_wgs84: list[float] | None = None,
                     date: str | None = None,
                     headway_start: str = "07:00:00",
                     headway_end: str = "19:00:00",
                     max_stops: int = 50_000) -> dict:
    """Fetch GTFS stops with service-quality attributes into ``output_path`` (GeoPackage).

    ``feed`` is one of de_fv / de_rv / de_nv / de_full (``feeds_catalog()`` lists them).
    ``bbox`` = [west, south, east, north] in WGS84 windows the feed — **strongly
    recommended** for de_nv / de_full (national, ~220 MB) so the service-stat
    computation stays tractable. Each stop point (EPSG:4326) carries, for a
    representative service ``date`` (default a Tuesday of the feed's first week):
    ``num_trips`` and ``num_routes`` that day, and ``mean_headway`` / ``max_headway`` /
    ``min_headway`` (minutes) plus ``start_time`` / ``end_time`` (service span) within
    the ``headway_start``–``headway_end`` window. Powers transit-coverage and
    service-quality maps.

    A **null** headway means none can be stated for that stop (fewer than three distinct
    departure times in the window), not a fast one — simultaneous departures are counted
    once, so no stop can ever score a 0-minute headway.
    """
    res, err = _load_feed(feed, cache_dir)
    if err:
        return err
    feed_obj, name, licence = res

    if bbox_wgs84:
        try:
            feed_obj = _restrict_to_bbox(feed_obj, bbox_wgs84)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"bbox restriction failed: "
                    f"{type(exc).__name__}: {exc}"}

    try:
        stops = feed_obj.get_stops(as_gdf=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"stop read failed: {type(exc).__name__}: {exc}"}
    if stops is None or stops.empty:
        return {"ok": False, "error": "no stops"
                + (" in the bbox" if bbox_wgs84 else ""), "bbox": bbox_wgs84}
    if not bbox_wgs84 and len(stops) > max_stops:
        return {"ok": False, "error": f"feed '{name}' has {len(stops)} stops nationwide "
                f"(> {max_stops}) — pass a bbox to window it.", "stops": len(stops)}

    svc_date = _pick_date(feed_obj, date)
    served = 0
    if svc_date:
        try:
            ss = _stop_service_stats(feed_obj, svc_date, headway_start, headway_end)
            stops = stops.merge(ss, on="stop_id", how="left")
            stops["num_trips"] = stops["num_trips"].fillna(0)
            served = int((stops["num_trips"] > 0).sum())
        except Exception as exc:  # noqa: BLE001
            # stats are best-effort — a stops-only layer is still useful
            svc_date = f"{svc_date} (stats unavailable: {type(exc).__name__})"

    # `restrict_to_area` keeps whole trips touching the bbox, so stops just outside it
    # survive on a retained line. Clip the final stop geometry to the bbox so the layer
    # is "stops in this area" — the service stats above already reflect the full line.
    if bbox_wgs84:
        w, s, e, n = bbox_wgs84
        stops = stops.cx[w:e, s:n]
        if stops.empty:
            return {"ok": False, "error": "no stops in the bbox", "bbox": bbox_wgs84}
        served = int((stops["num_trips"] > 0).sum()) if "num_trips" in stops else served

    stops.to_file(output_path, driver="GPKG", layer="stops")
    no_service = _no_service_warning(feed_obj, svc_date, int(len(stops))) if not served else None
    return {
        "ok": True,
        "output": str(output_path),
        "dataset": "gtfs_stops",
        "feed": name,
        "service_date": svc_date,
        "stops": int(len(stops)),
        "stops_with_service": served,
        **({"warning": no_service} if no_service else {}),
        **_stop_composition(stops),
        "crs": "EPSG:4326",
        "licence": licence,
        "note": "GTFS stops (EPSG:4326) with per-stop service stats for the given date; "
        "headways in minutes over the headway window. An EMPTY mean_headway means no "
        "headway can be stated (fewer than 3 distinct departures in the window) — such "
        "a stop is not frequently served, so keep `mean_headway <= n` filters as they "
        "are and never treat empty as 0. Reproject to a metric CRS before "
        "distance/coverage work. These rows are NOT filtered by mode — a local feed "
        "carries bus, tram and urban rail together, and there is no mode filter here, "
        "so a question about *bus* stops cannot be answered from this layer alone "
        "(OSM `highway=bus_stop` can).",
    }


def _stop_composition(stops) -> dict:
    """How many rows are stations and how many are platforms of one.

    A GTFS layer counts **rows**, and a row is a platform as often as it is a stop:
    the 98 rows inside Regensburg's Innenstadt on 2026-08-23 were 22 stations plus 76
    of their platforms — one interchange contributed 32 rows by itself. Which number
    answers "how many stops" is the asker's call (Steig, Halt and Haltestelle are not
    the same thing), so this reports the split instead of picking a side. Without it
    the choice gets made silently, by whoever calls `len()` first.

    Absent columns simply yield no keys — an older or trimmed feed stays valid.
    """
    import pandas as pd

    out: dict = {}
    if "location_type" in stops:
        lt = pd.to_numeric(stops["location_type"], errors="coerce").fillna(0)
        out["stations"] = int((lt == 1).sum())
        out["platforms_and_stops"] = int((lt == 0).sum())
    if "parent_station" in stops:
        parent = stops["parent_station"].astype("string").fillna("")
        out["rows_with_parent_station"] = int((parent.str.strip() != "").sum())
    if out.get("rows_with_parent_station"):
        out["counting_hint"] = (
            f"{len(stops)} rows = {out.get('stations', 0)} station(s) + "
            f"{out['rows_with_parent_station']} platform row(s) belonging to one. "
            "Count rows for platforms/quays, count stations (location_type=1) or "
            "distinct parent_station for stops — and say which you counted."
        )
    return out


def _route_lines_and_stats(feed_obj, date: str, headway_start: str, headway_end: str,
                           bbox_wgs84=None):
    """Route lines + per-route service stats for ``date`` from the day's active trips.

    The wired DACH feeds have **no ``shapes.txt``**, so gtfs-kit can't geometrise routes;
    instead each route's line is the alignment of its **representative (longest) active
    trip** — a polyline connecting that trip's stops in ``stop_sequence`` order (not the
    exact track, but the served corridor). Stats (num_trips, num_stops, headway from trip
    start times) are self-computed like the stop stats (honours ``calendar_dates``).
    Returns an EPSG:4326 GeoDataFrame (LineString), clipped to ``bbox`` when given.
    """
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import LineString

    empty_cols = ["line", "route_id", "route_short_name", "route_long_name", "route_type",
                  "num_trips", "num_stops", "mean_headway", "min_headway",
                  "max_headway", "geometry"]
    active = set(feed_obj.get_trips(date)["trip_id"])
    trips = feed_obj.trips[feed_obj.trips["trip_id"].isin(active)][["trip_id", "route_id"]].copy()
    if trips.empty:
        return gpd.GeoDataFrame(columns=empty_cols, geometry="geometry", crs="EPSG:4326")

    # Group by *line*, not by route_id. On the Swiss geOps feed route_id is minted per
    # trip (882 301 of them, median one trip each), so grouping by it would emit one
    # "route line" per departure: ~882 000 near-identical polylines for ~1 740 real
    # lines. `route_id` below therefore carries the line key; the representative feed
    # route_id of each line is kept alongside as `route_ids` for traceability.
    route_line = _route_line_key(feed_obj)
    trips["_feed_route_id"] = trips["route_id"]
    trips["route_id"] = trips["route_id"].map(route_line)

    st = feed_obj.stop_times[feed_obj.stop_times["trip_id"].isin(active)].copy()
    st["dep"] = _hms_to_seconds(st["departure_time"])
    st = st.merge(trips, on="trip_id", how="left")
    coords = feed_obj.stops.set_index("stop_id")[["stop_lon", "stop_lat"]]
    st = st.join(coords, on="stop_id")

    # per-route stats
    num_trips = trips.groupby("route_id")["trip_id"].nunique()
    num_stops = st.groupby("route_id")["stop_id"].nunique()
    # headway from trip start times within the window
    start = st.groupby("trip_id")["dep"].min().reset_index().merge(trips, on="trip_id")
    ws = _hms_to_seconds(pd.Series([headway_start])).iloc[0]
    we = _hms_to_seconds(pd.Series([headway_end])).iloc[0]
    win = start[(start["dep"] >= ws) & (start["dep"] <= we)].sort_values(
        ["route_id", "dep"])
    g = (win.groupby("route_id")["dep"].diff() / 60.0).groupby(win["route_id"])
    hw = g.agg(["mean", "min", "max"]).rename(columns={
        "mean": "mean_headway", "min": "min_headway", "max": "max_headway"})

    # representative (longest) active trip per route → its stop-sequence polyline
    nstops = st.groupby("trip_id").size().rename("n").reset_index().merge(trips, on="trip_id")
    rep = nstops.sort_values("n").groupby("route_id").tail(1)
    rep_ids = set(rep["trip_id"])
    rep_st = st[st["trip_id"].isin(rep_ids)].sort_values(["trip_id", "stop_sequence"])
    trip_route = trips.set_index("trip_id")["route_id"].to_dict()
    line_by_route: dict = {}
    for tid, grp in rep_st.groupby("trip_id"):
        pts = grp[["stop_lon", "stop_lat"]].dropna().to_numpy()
        # drop consecutive duplicate points
        uniq = [tuple(p) for i, p in enumerate(pts) if i == 0 or tuple(p) != tuple(pts[i - 1])]
        if len(uniq) >= 2:
            line_by_route[trip_route[tid]] = LineString(uniq)

    routes = feed_obj.routes.copy()
    for c in ("route_short_name", "route_long_name", "route_type"):
        if c not in routes.columns:
            routes[c] = None
    # One row per line. `line` is the grouping identity; `route_id` stays a real feed
    # id (the first one carrying that line) so a join back into the feed still lands
    # somewhere real — but the stats beside it cover the whole line, not that one id.
    routes["line"] = routes["route_id"].map(route_line)
    routes = routes.drop_duplicates("line")
    routes = routes[routes["line"].isin(line_by_route)].copy()
    routes = routes[["line", "route_id", "route_short_name", "route_long_name", "route_type"]]
    routes["num_trips"] = routes["line"].map(num_trips).fillna(0).astype(int)
    routes["num_stops"] = routes["line"].map(num_stops).fillna(0).astype(int)
    routes = routes.merge(hw, left_on="line", right_index=True, how="left")
    routes["geometry"] = routes["line"].map(line_by_route)
    gdf = gpd.GeoDataFrame(routes, geometry="geometry", crs="EPSG:4326")
    if bbox_wgs84 is not None and not gdf.empty:
        from shapely.geometry import box

        gdf = gpd.clip(gdf, box(*bbox_wgs84))
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
    return gdf


def fetch_gtfs_routes(feed: str, output_path: str, cache_dir: str,
                      bbox_wgs84: list[float] | None = None,
                      date: str | None = None,
                      headway_start: str = "07:00:00",
                      headway_end: str = "19:00:00",
                      max_routes: int = 20_000) -> dict:
    """Fetch GTFS routes as lines with per-route service stats into ``output_path``.

    ``feed`` = a registered feed (de_*/ch_*) or a local GTFS zip path. ``bbox`` =
    [west, south, east, north] in WGS84 windows the feed (pass it for the national
    feeds; route lines are then clipped to the bbox). ``date`` picks the service day
    (else a representative weekday). Each route line (EPSG:4326) carries
    ``route_short_name`` / ``route_long_name`` / ``route_type`` and ``num_trips`` /
    ``num_stops`` / ``mean_headway`` (min) for that day. Because the DACH feeds ship no
    ``shapes.txt``, the geometry is the representative (longest) trip's stop-sequence
    polyline — the served corridor, not the exact track. Complements
    ``fetch_gtfs_stops`` (the network's lines vs its stops).

    **One row per line, not per ``route_id``.** Some feeds mint a route per trip (the
    Swiss geOps feed: 882 301 route_ids for 2 046 678 trips), which by route_id would
    return ~9 400 near-identical polylines for the Bern area instead of 82. Rows are
    keyed by ``line`` (agency + ``route_short_name``, falling back to ``route_id``
    where no short name exists); ``route_id`` keeps a real feed id of that line so a
    join back into the feed lands somewhere, but the stats beside it cover the whole
    line, not that single id.
    """
    res, err = _load_feed(feed, cache_dir)
    if err:
        return err
    feed_obj, name, licence = res

    if not bbox_wgs84 and len(feed_obj.routes) > max_routes:
        return {"ok": False, "error": f"feed '{name}' has {len(feed_obj.routes)} routes "
                f"nationwide (> {max_routes}) — pass a bbox to window it.",
                "routes": len(feed_obj.routes)}
    if bbox_wgs84:
        try:
            feed_obj = _restrict_to_bbox(feed_obj, bbox_wgs84)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"bbox restriction failed: "
                    f"{type(exc).__name__}: {exc}"}

    svc_date = _pick_date(feed_obj, date)
    if not svc_date:
        return {"ok": False, "error": "feed has no service dates"}
    try:
        gdf = _route_lines_and_stats(feed_obj, svc_date, headway_start, headway_end,
                                     bbox_wgs84=bbox_wgs84)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"route build failed: {type(exc).__name__}: {exc}"}
    if gdf.empty:
        return {"ok": False, "error": "no routes"
                + (" in the bbox" if bbox_wgs84 else ""), "bbox": bbox_wgs84}

    gdf.to_file(output_path, driver="GPKG", layer="routes")
    return {
        "ok": True,
        "output": str(output_path),
        "dataset": "gtfs_routes",
        "feed": name,
        "service_date": svc_date,
        "routes": int(len(gdf)),
        "crs": "EPSG:4326",
        "licence": licence,
        "note": "GTFS route lines (EPSG:4326) — the representative (longest) trip's "
        "stop-sequence polyline per LINE (no shapes.txt in the DACH feeds), with "
        "num_trips / num_stops / mean_headway for the date. One row per line, keyed by "
        "the `line` column: some feeds mint one route_id per trip, so counting or "
        "drawing per route_id would multiply a single line into hundreds. `route_id` "
        "is one real feed id of that line; the stats cover the whole line. Reproject "
        "to metric before length/distance work.",
    }
