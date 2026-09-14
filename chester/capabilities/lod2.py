"""GeoLod2Capability — authoritative German building heights from open LoD2.

The real answer to "building heights": the Bundesländer publish **LoD2 building
models** as open data, each building carrying a laser-measured
``bldg:measuredHeight``. That is the true, per-building height — superior to
DSM−DTM raster differencing and infinitely better than Copernicus GLO-30
(``fetch_dem``, ~30 m, useless for buildings). This capability is the thin agent
layer over ``chester/lod2.py`` (the registry + CityGML parser + fetch):

- ``lod2_sources()`` — the per-Bundesland coverage (which states are wired vs.
  documented-only) and their licences.
- ``fetch_lod2(bbox, output_path, state?, street?)`` — fetch the covering LoD2
  tiles, parse footprint + measured height + address, clip to the bbox (and an
  optional street), write a GeoPackage in a metric CRS, with a provenance sidecar.

**Seit Phase KM Schritt 1 nur noch der Adapter.** Werkzeuge und Instruktionsblock
stehen in `chester/lod2tools.py` — rahmenneutral, ohne `pydantic_ai`, damit derselbe
Satz später den MCP-Server bedient, ohne dass zwei Werkzeugoberflächen auseinander
laufen (`internal/chester-mcp.md` §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.lod2tools import INSTRUCTIONS, build_tools
from chester.workspace import DEFAULT_WORKSPACE


@dataclass
class GeoLod2Capability(AbstractCapability[Any]):
    """Fetch authoritative building heights from the Bundesländer's open LoD2."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=build_tools(self.workspace))
