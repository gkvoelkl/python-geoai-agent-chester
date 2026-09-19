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

**Diese Datei ist seit dem 2026-09-13 nur noch der Adapter.** Werkzeuge und
Instruktionsblock stehen in `chester/coretools.py` — rahmenneutral, ohne
`pydantic_ai`, damit derselbe Satz später auch den MCP-Server bedienen kann, ohne
dass zwei Werkzeugoberflächen auseinanderlaufen (`internal/chester-mcp.md` §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.coretools import INSTRUCTIONS, build_tools
from chester.workspace import DEFAULT_WORKSPACE


@dataclass
class GeoCoreCapability(AbstractCapability[Any]):
    """Raster-, Terrain- und Netzwerkoperationen als Werkzeuge."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=build_tools(self.workspace))
