"""GeoInventoryCapability — awareness of, and bounds on, the local GeoCache.

Chester is strong at *processing* geodata but blind to *what it already has*.
This capability gives it a disk-reconciled inventory of cached datasets (the
GeoCache), so it stops guessing file names, and an expiry mechanism so the cache
stays bounded. All the real work lives in :class:`chester.geocache.GeoCache`
(no SelmaKit dependency, shared with the ``data.py`` CLI); this is the thin
agent-facing layer: three tools and a prompt summary of recent datasets.

**Since Phase KM step 1 only the adapter.** Tools and instruction block live in
`chester/inventorytools.py` — framework-neutral, without `pydantic_ai`
(`internal/chester-mcp.md` §7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.geocache import DEFAULT_TTL_DAYS
from chester.inventorytools import INSTRUCTIONS, build_tools
from chester.workspace import DEFAULT_WORKSPACE


@dataclass
class GeoInventoryCapability(AbstractCapability[Any]):
    """Inventory + expiry over the workspace GeoCache."""

    workspace: str = DEFAULT_WORKSPACE
    roots: list[str] = field(default_factory=list)
    default_ttl_days: int = DEFAULT_TTL_DAYS
    ttl_by_source: dict[str, int] = field(default_factory=dict)

    def get_instructions(self):
        """Static text — deliberately without the cache listing.

        The listing used to be appended here, which made the system prompt change
        after every tool that wrote a layer. Ollama re-reads a prompt from the
        first differing character onward, so each write cost a full re-read of the
        rest of the instructions, all tool definitions *and* the whole
        conversation so far. `geocache_list` answers the same question on demand.
        """

        def _instructions(ctx: RunContext[Any]) -> str:
            return INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(
            tools=build_tools(
                self.workspace, self.roots, self.default_ttl_days, self.ttl_by_source
            )
        )
