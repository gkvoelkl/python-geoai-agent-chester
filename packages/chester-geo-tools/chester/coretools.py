"""The eleven raster/terrain/network tools as **framework-neutral** wrappers.

The prototype for the rework `internal/chester-mcp.md` §7 asks for: tools described
*once* and offered by **two** adapters — today `GeoCoreCapability` (pydantic-ai), and
the MCP server for foreign clients. Without this layer there would be two tool surfaces
drifting apart; exactly the drift `evals.py`/`testprompt.py` avoid by sharing code.

**Pure like the cores**, and that is the load-bearing property: no `pydantic_ai`, no
`selmakit`, no import from `chester.capabilities`. What stands here can be plugged into
any framework — or none.

**The docstring is the tool description.** Both adapters read it; for the MCP server it
is the only text channel that demonstrably reaches the model (measured 2026-09-13,
`internal/chester-mcp.md` §4a). It therefore describes what the tool does and does
*not* do — and holds no instructions to the model: outside Chester those are read as
data, not as rules (§4b).
"""

from __future__ import annotations

from collections.abc import Callable

from chester import networkops, rasterops, terrainops

#: The capability's instruction block. It lives here because it describes the *tools*,
#: not the framework — the MCP server can ignore it (it has no place for it, see §4a),
#: Chester's capability passes it on.
INSTRUCTIONS = """\
## Raster, terrain and network (no QGIS needed)

Eleven checked operations, each one call, paths in and paths out. They report what
went in and what came out, refuse work that would be silently wrong, and stamp
provenance on every file they write.

**Raster** — `rasterize` (burn a vector layer into a grid), `sample_raster` (the
raster's value at each point), `zonal_stats` (mean/min/max/sum/count per polygon),
`raster_calc` (a numpy expression over aligned rasters). nodata never enters a
statistic, every zone reports its `coverage`, and a zone the raster does not reach
gets `null` — never 0. "No supermarkets here" and "the raster does not cover this
place" are different answers.

**Terrain** — `slope`, `aspect`, `hillshade`, `ruggedness` need nothing but the DEM.
A hillshade is a rendering, not a measurement: never read heights or slopes off it.
`fill_sinks` and `flow_accumulation` need GRASS and say so when it is absent —
**fill the sinks BEFORE you accumulate flow**, or the drainage network stops at the
first depression.

**Network** — `service_area`: the area reachable from a point within a time budget
along the streets, not as the crow flies. It reports how far the start had to snap
to the network and how the isochrone compares to a straight-line circle of the same
reach, so a result that did not really use the network is visible in the numbers.

**A travel time is never a buffer.** "Within a 10-minute walk", "15-minute city",
"catchment of a stop" — all of these are `service_area` on a street network
(`osm_features(tags={"highway": true})`, reprojected to a metric CRS). A
`vector_buffer` of "about 800 m" for the same question ignores every river, railway
and dead end, and overstates reach; it is the wrong answer, not a rough one. Measured
2026-09-21: a run answered "supermarkets within a 10-minute walk" with an 800 m
buffer. If no network can be fetched, say the travel-time question cannot be answered
— do not substitute a radius.

All of them are bound in the `geo_python_run` namespace under the same names, for
when several steps in one snippet are cheaper than several calls.\
"""


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """The eleven tools, bound to ``workspace`` — in calling order.

    Closures rather than a class: every adapter gets plain functions with a docstring
    and a dict return; neither `FunctionToolset` nor `FastMCP.tool` needs more.
    """
    ws = workspace

    # ── Raster ──────────────────────────────────────────────────────────────
    def rasterize(vector_path: str, output_path: str, resolution: float,
                  column: str | None = None, burn: float = 1.0) -> dict:
        """Burn a vector layer into a new raster grid of ``resolution`` map units.

        ``column`` burns that attribute per feature; without it every feature
        burns ``burn``. Refuses a geographic CRS — a resolution in degrees is not
        a cell size in metres.
        """
        return rasterops.rasterize(vector_path, output_path, resolution=resolution,
                                   column=column, burn=burn, workspace=ws)

    def sample_raster(raster_path: str, points_path: str, output_path: str,
                      column: str = "value") -> dict:
        """Write the raster's value at each point into a new column.

        Points outside the raster and points on nodata get ``null``, never 0;
        ``without_value`` counts them.
        """
        return rasterops.sample_raster(raster_path, points_path, output_path,
                                       column=column, workspace=ws)

    def zonal_stats(raster_path: str, zones_path: str, output_path: str,
                    stat: str = "mean", column: str | None = None) -> dict:
        """Summarise the raster inside each zone (mean/min/max/sum/count).

        Masks nodata out rather than averaging it in, and adds a ``coverage``
        column: the share of each zone's cells that carried data.
        """
        return rasterops.zonal_stats(raster_path, zones_path, output_path,
                                     stat=stat, column=column, workspace=ws)

    def raster_calc(output_path: str, expression: str, a: str,
                    b: str | None = None, c: str | None = None) -> dict:
        """Evaluate a numpy expression over aligned rasters, e.g. "(a - b) / (a + b)".

        ``a``/``b``/``c`` are raster paths bound to those names. Grids that do not
        line up are refused rather than broadcast into a wrong result.
        """
        rasters = {k: v for k, v in (("a", a), ("b", b), ("c", c)) if v}
        return rasterops.raster_calc(output_path, expression, workspace=ws, **rasters)

    # ── Terrain ─────────────────────────────────────────────────────────────
    def slope(dem_path: str, output_path: str) -> dict:
        """Slope in **degrees** from a DEM whose units are metres."""
        return terrainops.slope(dem_path, output_path, workspace=ws)

    def aspect(dem_path: str, output_path: str) -> dict:
        """Aspect in degrees clockwise from north (0 = N, 90 = E)."""
        return terrainops.aspect(dem_path, output_path, workspace=ws)

    def hillshade(dem_path: str, output_path: str, azimuth: float = 315.0,
                  altitude: float = 45.0) -> dict:
        """Shaded relief, 0–255 — a picture, never a measurement.

        Say so when you report it: it looks like terrain data and carries none.
        """
        return terrainops.hillshade(dem_path, output_path, azimuth=azimuth,
                                    altitude=altitude, workspace=ws)

    def ruggedness(dem_path: str, output_path: str) -> dict:
        """Terrain Ruggedness Index: mean height difference to the 8 neighbours."""
        return terrainops.ruggedness(dem_path, output_path, workspace=ws)

    def fill_sinks(dem_path: str, output_path: str) -> dict:
        """Fill depressions so water can leave every cell (needs GRASS).

        Do this **before** accumulating flow — an unfilled sink swallows the flow
        that should have continued downstream.
        """
        return terrainops.fill_sinks(dem_path, output_path, workspace=ws)

    def flow_accumulation(dem_path: str, output_path: str) -> dict:
        """Accumulated flow per cell (needs GRASS). Feed it a **filled** DEM.

        The values count upslope cells, not litres.
        """
        return terrainops.flow_accumulation(dem_path, output_path, workspace=ws)

    # ── Netzwerk ────────────────────────────────────────────────────────────
    def service_area(network_path: str, output_path: str, start_lon: float,
                     start_lat: float, minutes: float, mode: str = "walk",
                     start_crs: str = "EPSG:4326") -> dict:
        """The area reachable from a point within ``minutes`` along the network.

        ``network_path`` is a LINE layer in a **metric** CRS.
        ``start_lon``/``start_lat`` are longitude then latitude — named separately
        so the order cannot be swapped. ``mode`` sets the speed (walk 4.5, bike
        15, drive 50 km/h).
        """
        return networkops.service_area(network_path, output_path,
                                       start_lon=start_lon, start_lat=start_lat,
                                       minutes=minutes, mode=mode,
                                       start_crs=start_crs, workspace=ws)

    return [rasterize, sample_raster, zonal_stats, raster_calc,
            slope, aspect, hillshade, ruggedness, fill_sinks,
            flow_accumulation, service_area]
