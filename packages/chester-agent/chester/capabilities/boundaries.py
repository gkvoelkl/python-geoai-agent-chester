"""GeoBoundariesCapability — official German/EU administrative boundaries (BKG).

The geometry half of the **official-statistics → choropleth** workflow. Chester's
statistics connectors deliver tables keyed by AGS / NUTS code but no geometry;
this capability fetches the authoritative boundary polygons (BKG open
Verwaltungsgebiete, DL-DE→BY 2.0) carrying exactly those keys, so a stats table
joins straight onto the polygons. Thin agent layer over ``chester/boundaries.py``:

- ``boundaries_levels()`` — the fetchable admin levels and their join keys.
- ``fetch_boundaries(level, output_path, match?, bbox?)`` — a boundary subset as a
  GeoPackage (with a provenance sidecar).

Joining a stats table onto the result is one call to ``vector_join`` (on AGS or
NUTS_CODE). That join is where these polygons are usually lost: a key read as a
number drops the leading zero of every Bavarian AGS, and the result looks complete
while being empty — which is why ``vector_join`` reports what it matched.

**Seit Phase KM Schritt 1 nur noch der Adapter.** Werkzeuge und Instruktionsblock
stehen in `chester/boundariestools.py` — rahmenneutral, ohne `pydantic_ai`, damit derselbe
Satz später den MCP-Server bedient, ohne dass zwei Werkzeugoberflächen auseinander
laufen (`internal/chester-mcp.md` §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.boundariestools import INSTRUCTIONS, build_tools
from chester.workspace import DEFAULT_WORKSPACE


@dataclass
class GeoBoundariesCapability(AbstractCapability[Any]):
    """Fetch official German/EU administrative boundaries from the BKG."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=build_tools(self.workspace))
