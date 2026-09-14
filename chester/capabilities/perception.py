"""PerceptionCapability — extract information from imagery via spectral indices.

This is the "AI perception" pillar in its lightweight, fully local
form: normalized-difference indices (NDWI for water, NDVI for vegetation) computed
with rasterio + numpy, then thresholded and polygonized into vector features. It
needs no GPU and no model download, and maps directly onto the classic
"NDVI / band combinations" perception tools.

Heavier learned perception (SAM/`samgeo`, Prithvi) is a documented future extension
— see the note in TODO.md. The tool interface here
(raster in → vector mask out) is the same shape those would slot into.

**Seit Phase KM Schritt 1 nur noch der Adapter.** Werkzeuge und Instruktionsblock
stehen in `chester/perceptiontools.py` — rahmenneutral, ohne `pydantic_ai`, damit derselbe
Satz später den MCP-Server bedient, ohne dass zwei Werkzeugoberflächen auseinander
laufen (`internal/chester-mcp.md` §7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.perceptiontools import INSTRUCTIONS, build_tools
from chester.workspace import DEFAULT_WORKSPACE


@dataclass
class PerceptionCapability(AbstractCapability[Any]):
    """Spectral-index perception: water (NDWI) and vegetation (NDVI)."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=build_tools(self.workspace))
