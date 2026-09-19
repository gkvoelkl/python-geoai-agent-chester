"""GeoValidationCapability — the mandatory correctness phase of the loop.

Geodata results are objectively right or wrong. Before Chester
reports a result, it should check that the CRS is appropriate and that the output
is plausible. These tools make that checkable rather than vibes-based.

geopandas/rasterio are imported lazily inside the tools so the agent still boots
fast and so a missing raster stack doesn't break vector workflows.

**Seit dem 2026-09-13 nur noch der Adapter.** Werkzeuge und Instruktionsblock stehen
in `chester/validationtools.py` — rahmenneutral, ohne `pydantic_ai`, damit derselbe
Satz später `validate_result` im MCP-Server bedient, ohne dass zwei
Werkzeugoberflächen auseinanderlaufen (`internal/chester-mcp.md` §7, Phase KM).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.validationtools import INSTRUCTIONS, build_tools
from chester.workspace import DEFAULT_WORKSPACE


@dataclass
class GeoValidationCapability(AbstractCapability[Any]):
    """CRS and plausibility checks for vector and raster outputs."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=build_tools(self.workspace))
