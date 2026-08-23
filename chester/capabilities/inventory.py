"""GeoInventoryCapability — awareness of, and bounds on, the local GeoCache.

Chester is strong at *processing* geodata but blind to *what it already has*.
This capability gives it a disk-reconciled inventory of cached datasets (the
GeoCache), so it stops guessing file names, and an expiry mechanism so the cache
stays bounded. All the real work lives in :class:`chester.geocache.GeoCache`
(no SelmaKit dependency, shared with the ``data.py`` CLI); this is the thin
agent-facing layer: three tools and a prompt summary of recent datasets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.geocache import DEFAULT_TTL_DAYS, GeoCache
from chester.workspace import DEFAULT_WORKSPACE

_INSTRUCTIONS_HEAD = """\
## GeoCache (what you already have)

Cached datasets live under the workspace and are tracked in a self-maintaining
inventory. Before downloading or generating data, check whether it already
exists:
- `geocache_list` — the inventory (name, kind, CRS, extent, age). Pass a
  `filter` substring to narrow it (place, CRS, "raster", …).
- `geocache_sync` — reconcile the inventory with disk and drop expired datasets.
  Runs automatically; call it only after large external changes.
- `geocache_note` — record what a dataset *is* (purpose/semantics) and, with
  `ttl_days`, pin how long to keep it. Scanning can read a layer's CRS and
  extent but not its meaning — that's what notes are for. A pinned TTL is
  marked `*` in the inventory and overrides the configured retention, so use it
  for a dataset that must outlive its source's normal lifetime (expensive to
  re-fetch, or needed later in a long task).

Datasets age out from their last use (cache stays bounded); re-downloads and
derived layers can always be recreated. Reach for a cached dataset by the name
`geocache_list` shows rather than inventing a path.

**Call `geocache_list` at the start of a task that needs data.** This section
deliberately does not name the cached datasets: the listing changes every time a
tool writes a layer, and a changing prompt costs a full re-read of everything
after it (measured 2026-08-22 on the local model: 0.1 s for an unchanged prompt
versus 52.8 s once one line in the middle differs). One tool call is cheaper.\
"""


@dataclass
class GeoInventoryCapability(AbstractCapability[Any]):
    """Inventory + expiry over the workspace GeoCache."""

    workspace: str = DEFAULT_WORKSPACE
    roots: list[str] = field(default_factory=list)
    default_ttl_days: int = DEFAULT_TTL_DAYS
    ttl_by_source: dict[str, int] = field(default_factory=dict)

    def _cache(self) -> GeoCache:
        return GeoCache(
            workspace=self.workspace,
            roots=self.roots,
            default_ttl_days=self.default_ttl_days,
            ttl_by_source=self.ttl_by_source,
        )

    def get_instructions(self):
        """Static text — deliberately without the cache listing.

        The listing used to be appended here, which made the system prompt change
        after every tool that wrote a layer. Ollama re-reads a prompt from the
        first differing character onward, so each write cost a full re-read of the
        rest of the instructions, all tool definitions *and* the whole
        conversation so far. `geocache_list` answers the same question on demand.
        """

        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS_HEAD

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        def geocache_list(filter: str | None = None) -> dict:
            """List cached datasets (syncs with disk first).

            Each entry reports name, kind, CRS, feature count / raster size,
            WGS84 extent, source, age and expiry. ``filter`` keeps only datasets
            whose name, note, CRS or kind contains the substring.
            """
            try:
                rows = self._cache().list(filter=filter)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {"ok": True, "count": len(rows), "datasets": rows}

        def geocache_sync() -> dict:
            """Reconcile the inventory with the files on disk and delete expired
            datasets. Returns counts of added / refreshed / dropped / expired."""
            try:
                summary = self._cache().sync()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {"ok": True, **summary}

        def geocache_note(
            path: str,
            note: str,
            layer: str | None = None,
            ttl_days: int | None = None,
        ) -> dict:
            """Attach a note (purpose/semantics) to a cached dataset, and
            optionally pin its retention with ``ttl_days``.

            ``path`` is the dataset name from the inventory (or a file path).
            For a multi-layer container, pass ``layer`` to target one layer.
            Also refreshes the dataset's last-used time.
            """
            try:
                return self._cache().note(path, note, layer=layer, ttl_days=ttl_days)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        return FunctionToolset(tools=[geocache_list, geocache_sync, geocache_note])
