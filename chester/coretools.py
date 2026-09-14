"""Die elf Raster-/Terrain-/Netzwerkwerkzeuge als **rahmenneutrale** Hüllen.

Prototyp für den Umbau, den `internal/chester-mcp.md` §7 verlangt: Werkzeuge, die
*ein* Mal beschrieben sind und von **zwei** Adaptern angeboten werden — heute von
`GeoCoreCapability` (pydantic-ai), später vom MCP-Server für fremde Clients. Ohne
diese Schicht gäbe es zwei Werkzeugoberflächen, die auseinanderlaufen; genau die
Drift, die `evals.py`/`testprompt.py` durch geteilten Code vermeiden.

**Rein wie die Kerne**, und das ist die tragende Eigenschaft: kein `pydantic_ai`,
kein `selmakit`, kein Import aus `chester.capabilities`. Was hier steht, lässt sich
von jedem Rahmen einhängen — oder von gar keinem.

**Der Docstring ist die Werkzeugbeschreibung.** Beide Adapter lesen ihn; für den
MCP-Server ist er der einzige Textkanal, der das Modell nachweislich erreicht
(gemessen 2026-09-13, `internal/chester-mcp.md` §4a). Er beschreibt deshalb, was das
Werkzeug tut und was es *nicht* tut — und enthält keine Anweisungen an das Modell:
Die werden ausserhalb von Chester als Daten gelesen, nicht als Regel (§4b).
"""

from __future__ import annotations

from collections.abc import Callable

from chester import networkops, rasterops, terrainops

#: Der Instruktionsblock der Fähigkeit. Er lebt hier, weil er die *Werkzeuge*
#: beschreibt, nicht den Rahmen — der MCP-Server kann ihn ignorieren (er hat keinen
#: Ort dafür, siehe §4a), Chesters Capability reicht ihn weiter.
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

All of them are bound in the `geo_python_run` namespace under the same names, for
when several steps in one snippet are cheaper than several calls.\
"""


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """Die elf Werkzeuge, an ``workspace`` gebunden — in Aufrufreihenfolge.

    Closures statt einer Klasse: Jeder Adapter bekommt schlichte Funktionen mit
    Docstring und Dict-Rückgabe, mehr braucht weder `FunctionToolset` noch
    `FastMCP.tool`.
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
