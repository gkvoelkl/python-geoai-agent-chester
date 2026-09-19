"""GeoCityModelCapability — 3D building models (CityJSON) and their 3D display.

The agent surface over ``chester/citymodel.py`` (the pure CityGML→CityJSON writer +
cjio reader + renderers). Turns the LoD2 building models (measured 3D geometry) into
CityJSON and into interactive 3D output:

- ``fetch_cityjson(bbox, output_path, state?)`` — fetch the covering open LoD2
  CityGML tiles (Bayern/NRW/Brandenburg/M-V), write CityJSON, clip to the bbox.
- ``fetch_swissbuildings3d(bbox, output_path)`` — the Swiss counterpart:
  swissBUILDINGS3D 3.0 (swisstopo, EPSG:2056) → CityJSON.
- ``render_buildings_3d(cityjson_path, output_path, style?)`` — CityJSON → a
  self-contained 3D HTML: ``"roofs"`` (three.js, real LoD2 shells) or ``"blocks"``
  (MapLibre 2.5D extrusion).
- ``cityjson_to_geopackage(cityjson_path, output_path)`` — CityJSON → a
  MultiPolygonZ GeoPackage (3D-capable).
{_QGIS_INTRO}No Java anywhere — Chester writes the CityJSON itself.

**Seit Phase KM Schritt 1 nur noch der Adapter.** Werkzeuge und Instruktionsblock
stehen in `chester/citymodeltools.py` — rahmenneutral, ohne `pydantic_ai`, damit derselbe
Satz später den MCP-Server bedient, ohne dass zwei Werkzeugoberflächen auseinander
laufen (`internal/chester-mcp.md` §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.citymodeltools import INSTRUCTIONS, build_tools
from chester.workspace import DEFAULT_WORKSPACE


@dataclass
class GeoCityModelCapability(AbstractCapability[Any]):
    """Fetch/build CityJSON 3D building models and render them in 3D."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=build_tools(self.workspace))
