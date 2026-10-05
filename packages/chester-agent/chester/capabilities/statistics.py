"""GeoStatisticsCapability — statistical-data connectors (Phase 5.8).

GeoBenchX's largest class of tasks joins a statistical table to an admin geometry
to make a thematic (choropleth) map. This capability fetches the *numbers*; the
join to geometry is a normal QGIS step.

Only **credential-free** sources are wired here. The three GENESIS-2020 sources
(Destatis Regionalstatistik / GENESIS-Online / Zensus 2022) were removed: their
REST API requires a registered account (a 10-char user id or a 32-char token),
and gating a core workflow behind per-machine credentials proved impractical.
What remains needs no login:

- ``eurostat`` — Eurostat dissemination API (JSON-stat). EU-wide, NUTS 0–3. Always
  on. Coarser than Gemeinde level.
- ``wikidata`` — Wikidata SPARQL. Germany, per Gemeinde/Kreis: population (P1082)
  and area (P2046) carrying the AGS key (P439) — the credential-free replacement
  for Regionalstatistik's Gemeinde-level population, sourced from official
  statistics. Search resolves a region name to its AGS/Kreisschlüssel via the
  MWAPI full-text index; the table query returns one row per Gemeinde under that
  prefix.
- ``worldbank`` — World Bank Indicators API (v2). Global, per country: ~1500 World
  Development Indicators keyed on the ISO-3 country code. Search greps the WDI
  catalog for an indicator code; the table fetches the most-recent non-empty value
  per country (``mrnev=1``), dropping aggregate rows (World, income groups).

Never invent numbers: if an authoritative value cannot be obtained, report the
blocker instead of fabricating a plausible one.

The connector *delivers the table*; joining it to geometry is one call to
``vector_join`` on the NUTS/AGS key, which reports how many rows actually matched. Every
tool returns ``{"ok": false, "error": …}`` instead of raising, so a network hiccup
never crashes the loop.

**Since Phase KM step 1 only the adapter.** Tools and instruction block live in
`chester/statisticstools.py` — framework-neutral, without `pydantic_ai`, so the same set
serves the MCP server without two tool surfaces drifting apart
(`internal/chester-mcp.md` §7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.statisticstools import INSTRUCTIONS, build_tools
from chester.workspace import DEFAULT_WORKSPACE


@dataclass
class GeoStatisticsCapability(AbstractCapability[Any]):
    """Fetch official statistics (Eurostat; credential-free)."""

    workspace: str = DEFAULT_WORKSPACE
    statistics: dict = field(default_factory=dict)

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=build_tools(self.workspace))
