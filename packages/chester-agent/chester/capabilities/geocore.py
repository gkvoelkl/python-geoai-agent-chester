"""GeoCoreCapability — raster, terrain and network as tools, without QGIS.

The sister of the nine vector tools on `VectorCapability`. All eleven here compute via
`chester.rasterops` / `terrainops` / `networkops` — pure cores on rasterio, numpy,
networkx and (for hydrology only) GRASS.

**Why they are tools and not only functions in the sandbox namespace.** Measured
2026-09-07 (`buffer-schools-500m`, QGIS off): the agent called `vector_split_by_geometry`
unprompted — a tool — and never touched the same operations in the namespace. Its own
snippets said "Since I can't call 'reproject' inside here" and "Attempting to see if
the tool 'reproject' is available in the scope". Ten snippet calls, all with
`outputs: []` and `calls: []`, no provenance sidecar. What is in the tool catalogue gets
used; what is only in the prose does not.

The names here are **short and identical to those in the sandbox namespace** —
`zonal_stats`, `slope`, `service_area` are unambiguous enough to stand without a
prefix. The nine vector operations carry `vector_*`, because `buffer`, `clip` and
`dissolve` would be too generic as bare tool names; in a snippet both spellings work.

**Since 2026-09-13 this file is only the adapter.** Tools and instruction block live in
`chester/coretools.py` — framework-neutral, without `pydantic_ai`, so the same set can
serve the MCP server without two tool surfaces drifting apart (`internal/chester-mcp.md`
§7).
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
    """Raster, terrain and network operations as tools."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=build_tools(self.workspace))
