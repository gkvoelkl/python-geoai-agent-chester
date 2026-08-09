"""GeoTransitCapability — public-transit (GTFS) connector.

The timetable-aware data type QGIS' single-mode network analysis can't provide.
Reaches the open GTFS feeds of the German-speaking region (start: the credential-free
German path via gtfs.de) and turns a feed into stop points carrying service-quality
attributes (trips/routes per day, mean headway, service span). Thin agent layer over
``chester/gtfs.py``:

- ``gtfs_feeds()`` — the available feeds and their sizes/licences.
- ``fetch_gtfs_stops(feed, output_path, bbox?, date?)`` — stops as a GeoPackage with
  per-stop service stats (with a provenance sidecar).

Downstream this is ordinary geodata: reproject to a metric CRS, map service quality
with ``render_map`` (e.g. graduated by ``num_trips`` / ``mean_headway``), or intersect
with ``walkability`` isochrones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import gtfs, provenance
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

_INSTRUCTIONS = """\
## Public transit (GTFS) — timetable-aware stops & service quality

For public-transport questions (stops, service frequency, transit coverage) use the
GTFS connector — QGIS network analysis is single-mode and can't see a timetable:

- `gtfs_feeds()` — the feeds. **Germany** (gtfs.de, credential-free): `de_fv` (long-
  distance rail, tiny), `de_rv` (regional rail), `de_nv` (local bus/tram/metro, national
  ~220 MB), `de_full` (everything). **Switzerland** (geOps, credential-free): `ch_rail`
  (~18 MB), `ch_bus`, `ch_full`. **Austria** `at_full` / `at_oebb` are **gated** (a
  terms-of-use confirmation) — not auto-downloadable; `gtfs_feeds()` shows their portal.
  For a gated / foreign / private feed, download the GTFS zip yourself and pass its
  **local path** as `feed` (the tool reads any local GTFS zip).
- `fetch_gtfs_stops(feed, output_path, bbox?, date?)` — stop points (EPSG:4326) with
  per-stop `num_trips` / `num_routes` for a representative day and `mean_headway` /
  `min_headway` / `max_headway` (minutes) + `start_time`/`end_time` (service span).
- `fetch_gtfs_routes(feed, output_path, bbox?, date?)` — route **lines** (EPSG:4326)
  with `route_short_name` / `route_long_name` / `route_type` + `num_trips` / `num_stops`
  / `mean_headway`. Use for the network's *lines* (vs `fetch_gtfs_stops` for its stops).
  The DACH feeds have no shapes.txt, so a line is the representative (longest) trip's
  stop-sequence polyline — the served corridor, not the exact track. **Caveat:** this is
  meaningful when the feed uses one `route_id` per line (the German `de_*` feeds — e.g.
  Regensburg → 97 lines, line 1 = 189 trips/day, ~5 min headway). The Swiss `ch_*`
  (geOps) feeds assign a `route_id` **per journey**, so routes explode (thousands, each
  `num_trips`=1) and are not line-level — for CH service quality prefer
  `fetch_gtfs_stops`.

The stops layer's columns are exactly: `stop_id`, `stop_name` (the label — use
**`stop_name`**, not `name`, in `render_map(fields=…)` and tooltips), `num_trips`,
`num_routes`, `mean_headway`, `min_headway`, `max_headway`, `start_time`, `end_time`.
Note `mean/min/max_headway` are **NaN** for a stop with a single trip that day (no
headway is defined) — treat that as "not frequently served", never as 0.

**Always pass a `bbox`** for `de_nv` / `de_full` (they are national) — the feed is cut
to the bbox before the service-stat computation. For a city, `geocode` it first and
pass its bbox. The stops are in **EPSG:4326** — reproject to a metric CRS
(`qgis_reproject`) before any distance/coverage measure. Map service quality with
`render_map` graduated by `num_trips` or `mean_headway`; with a sequential palette
(e.g. YlOrRd) the **high** end is the dark/red colour (many departures), the light end
is low — describe the legend that way round. Combine with `qgis_service_area` /
`walkability` isochrones for accessibility.\
"""


# Emitted whenever a national feed is windowed by a raw bbox: a rectangle over-covers
# a named city. GTFS has no place= clip (feeds are national), so the fix is bbox-fetch
# then clip the result against the admin boundary.
_BBOX_CLIP_HINT = (
    "these GTFS features were windowed by a BBOX (a rectangle), which reaches into "
    "neighbouring municipalities — for a NAMED city (e.g. Regensburg) that over-covers "
    "the area. GTFS feeds have no place= clip, so to restrict to the actual city: call "
    "geocode(query, output_path=\"boundary.gpkg\") for the admin polygon, then "
    "qgis_clip this layer against it (reproject both to the same metric CRS first) "
    "before mapping/counting. Keep the bbox result only if an explicit coordinate "
    "window was intended."
)


@dataclass
class GeoTransitCapability(AbstractCapability[Any]):
    """Fetch open GTFS public-transit feeds as geodata with service-quality stats."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace

        def gtfs_feeds() -> dict:
            """List the available GTFS feeds (name, country, size, licence,
            credential-free?). DE (gtfs.de) + CH (geOps) are credential-free; AT is
            gated (download manually, pass the local zip path)."""
            return {"ok": True, "feeds": gtfs.feeds_catalog(),
                    "note": "DE (gtfs.de) + CH (geOps) are credential-free. AT is gated. "
                    "Pass a bbox for the national feeds; a local GTFS zip path also works."}

        def fetch_gtfs_stops(
            feed: str,
            output_path: str,
            bbox: list[float] | None = None,
            date: str | None = None,
        ) -> dict:
            """Fetch GTFS stops with per-stop service-quality stats into a GeoPackage.

            ``feed`` = a registered feed (de_fv/de_rv/de_nv/de_full, ch_rail/ch_bus/
            ch_full) **or a local path to a GTFS zip** (for a gated/foreign feed).
            ``bbox`` = [west, south, east, north] in WGS84 windows the feed — pass it for
            the national feeds. ``date`` (YYYY-MM-DD, optional) picks the service day,
            else a representative weekday. Each stop point (EPSG:4326) carries num_trips /
            num_routes and mean/min/max headway (minutes) + the service span for that day.
            Reproject to a metric CRS before distance work; map with render_map
            (graduated by num_trips or mean_headway).
            """
            output_path = resolve_path(output_path, ws)
            cache_dir = str(resolve_path("_gtfs", ws))
            if feed.strip().lower().endswith(".zip"):  # a local GTFS zip path
                feed = str(resolve_path(feed, ws))
            try:
                r = gtfs.fetch_gtfs_stops(feed, output_path, cache_dir,
                                          bbox_wgs84=bbox, date=date)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path, source="connector/gtfs", tool="fetch_gtfs_stops",
                    query={"feed": r["feed"], "date": r.get("service_date"),
                           "bbox": bbox},
                    crs=r.get("crs"), licence=r.get("licence"),
                )
                if bbox:
                    r["warning"] = (
                        (r["warning"] + " " if r.get("warning") else "") + _BBOX_CLIP_HINT
                    )
            return r

        def fetch_gtfs_routes(
            feed: str,
            output_path: str,
            bbox: list[float] | None = None,
            date: str | None = None,
        ) -> dict:
            """Fetch GTFS routes as lines with per-route service stats into a GeoPackage.

            ``feed`` = a registered feed (de_*/ch_*) or a local GTFS zip path. ``bbox`` =
            [west, south, east, north] in WGS84 windows the feed (pass it for national
            feeds; lines are clipped to it). ``date`` picks the service day. Each route
            line (EPSG:4326) carries route_short_name / route_long_name / route_type and
            num_trips / num_stops / mean_headway (min). The DACH feeds ship no shapes.txt,
            so the geometry is the representative (longest) trip's stop-sequence polyline
            (the served corridor, not the exact track). Complements fetch_gtfs_stops.
            """
            output_path = resolve_path(output_path, ws)
            cache_dir = str(resolve_path("_gtfs", ws))
            if feed.strip().lower().endswith(".zip"):  # a local GTFS zip path
                feed = str(resolve_path(feed, ws))
            try:
                r = gtfs.fetch_gtfs_routes(feed, output_path, cache_dir,
                                           bbox_wgs84=bbox, date=date)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path, source="connector/gtfs", tool="fetch_gtfs_routes",
                    query={"feed": r["feed"], "date": r.get("service_date"),
                           "bbox": bbox},
                    crs=r.get("crs"), licence=r.get("licence"),
                )
                if bbox:
                    r["warning"] = (
                        (r["warning"] + " " if r.get("warning") else "") + _BBOX_CLIP_HINT
                    )
            return r

        return FunctionToolset(tools=[gtfs_feeds, fetch_gtfs_stops, fetch_gtfs_routes])
