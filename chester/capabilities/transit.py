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

**Seit Phase KM Schritt 1 nur noch der Adapter.** Werkzeuge und Instruktionsblock
stehen in `chester/transittools.py` — rahmenneutral, ohne `pydantic_ai`, damit derselbe
Satz später den MCP-Server bedient, ohne dass zwei Werkzeugoberflächen auseinander
laufen (`internal/chester-mcp.md` §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.transittools import INSTRUCTIONS, build_tools
from chester.workspace import DEFAULT_WORKSPACE


@dataclass
class GeoTransitCapability(AbstractCapability[Any]):
    """Fetch open GTFS public-transit feeds as geodata with service-quality stats."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=build_tools(self.workspace))
