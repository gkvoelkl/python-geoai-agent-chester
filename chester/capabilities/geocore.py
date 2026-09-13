"""GeoCoreCapability — Raster, Terrain und Netzwerk als Werkzeuge, ohne QGIS.

Die Schwester der neun Vektorwerkzeuge auf der `VectorCapability`. Alle elf hier
rechnen über `chester.rasterops` / `terrainops` / `networkops` — reine Kerne auf
rasterio, numpy, networkx und (nur für Hydrologie) GRASS.

**Warum sie Werkzeuge sind und nicht nur Funktionen im Sandbox-Namensraum.**
Gemessen 2026-09-07 (`buffer-schools-500m`, QGIS abgeschaltet): Der Agent rief
`vector_split_by_geometry` ungefragt auf — ein Werkzeug — und rührte dieselben
Operationen im Namensraum kein einziges Mal an. In seinen eigenen Schnipseln stand
„Since I can't call 'reproject' inside here" und „Attempting to see if the tool
'reproject' is available in the scope". Zehn Schnipselaufrufe, durchgehend
`outputs: []` und `calls: []`, kein Provenienz-Sidecar. Was im Werkzeugkatalog steht,
wird benutzt; was nur in der Prosa steht, nicht.

Die Namen sind hier **kurz und identisch mit denen im Sandbox-Namensraum** —
`zonal_stats`, `slope`, `service_area` sind eindeutig genug, um ohne Präfix zu
stehen. Die neun Vektoroperationen tragen `vector_*`, weil `buffer`, `clip` und
`dissolve` als blanke Werkzeugnamen zu allgemein wären; im Schnipsel greifen dort
beide Schreibweisen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import networkops, rasterops, terrainops
from chester.workspace import DEFAULT_WORKSPACE

_INSTRUCTIONS = """\
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

All of them are bound in the `geo_python_run` namespace under the same names, for
when several steps in one snippet are cheaper than several calls.\
"""


@dataclass
class GeoCoreCapability(AbstractCapability[Any]):
    """Raster-, Terrain- und Netzwerkoperationen als Werkzeuge."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace

        # ── Raster ──────────────────────────────────────────────────────────
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

        # ── Terrain ─────────────────────────────────────────────────────────
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

        # ── Netzwerk ────────────────────────────────────────────────────────
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

        return FunctionToolset(
            tools=[rasterize, sample_raster, zonal_stats, raster_calc,
                   slope, aspect, hillshade, ruggedness, fill_sinks,
                   flow_accumulation, service_area]
        )
