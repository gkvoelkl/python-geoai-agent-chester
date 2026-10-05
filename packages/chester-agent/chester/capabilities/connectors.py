"""GeoConnectorsCapability — container connectors (GeoPackage / SpatiaLite / PostGIS).

The *query* connectors (geocode, OSM, STAC, DEM) produce one dataset per request
and live in ``discovery.py``. **Container** connectors are different: they hold an
*enumerable* set of existing datasets you **list, then pull** (doc/geodata-concept
§3.2). This capability gives the agent the uniform trio for them —

- `geoconnectors_list()`            — what sources are reachable (and their kind)
- `geodatasets_list(connector)`     — the layers/tables a container exposes
- `geodataset_describe(connector, dataset)` — one dataset's columns + extent
- `geodataset_fetch(connector, dataset, output, bbox?, where?)` — pull a subset
  into a cache GeoPackage (where it ages like any download, with a sidecar)

Two backends:
- **File containers** (`.gpkg`, `.sqlite`/SpatiaLite): zero-config, read via OGR
  (pyogrio/geopandas). No raw SQL — bbox is pushed to OGR as a numeric window and
  attribute `where` is applied in pandas, so a model-generated filter can't inject.
- **PostGIS**: configured by DSN + schema. Read-only, via `GeoDataFrame.from_postgis`
  with **bound parameters**, the table **whitelisted** to what `geodatasets_list`
  returns, and a parameterised `ST_MakeEnvelope(..., srid)` bbox (doc §4.3 safety).
  sqlalchemy/psycopg are imported lazily, so the file path works without them.

The capability is **inert when unconfigured** (no roots, no PostGIS DSN): it still
lets the user name a file container by path, but advertises nothing.

**Since Phase KM step 1 only the adapter.** Tools and instruction block live in
`chester/connectorstools.py` — framework-neutral, without `pydantic_ai`, so the same set
serves the MCP server without two tool surfaces drifting apart
(`internal/chester-mcp.md` §7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.connectorstools import INSTRUCTIONS, build_tools
from chester.workspace import DEFAULT_WORKSPACE


@dataclass
class GeoConnectorsCapability(AbstractCapability[Any]):
    """List and pull from container connectors (GeoPackage / SpatiaLite / PostGIS)."""

    workspace: str = DEFAULT_WORKSPACE
    roots: list[str] = field(default_factory=list)
    postgis: dict | None = None  # {"dsn": ..., "schema": ...}

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=build_tools(self.workspace, self.roots, self.postgis))
