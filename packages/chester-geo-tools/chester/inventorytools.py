"""The three GeoCache tools as **framework-neutral** wrappers.

Phase KM, step 1. The real work lives in :class:`chester.geocache.GeoCache` anyway —
this is only the agent-facing layer: list, sync, annotate.

**Why this worked despite a class method.** `GeoInventoryCapability._cache()` looked
like state but is a pure **factory**: it builds a fresh `GeoCache` from four fields. As
a closure over the same four values the behaviour stays identical — no object travels
between calls. `connectors` and `statistics` are different; they carry real state.
"""

from __future__ import annotations

from collections.abc import Callable

from chester.geocache import DEFAULT_TTL_DAYS, GeoCache

INSTRUCTIONS = """\
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


def build_tools(
    workspace: str,
    roots: list[str] | None = None,
    default_ttl_days: int = DEFAULT_TTL_DAYS,
    ttl_by_source: dict[str, int] | None = None,
) -> list[Callable[..., dict]]:
    """The three GeoCache tools, bound to the workspace and retention rules."""

    def _cache() -> GeoCache:
        return GeoCache(
            workspace=workspace,
            roots=roots or [],
            default_ttl_days=default_ttl_days,
            ttl_by_source=ttl_by_source or {},
        )

    def geocache_list(filter: str | None = None) -> dict:
        """List cached datasets (syncs with disk first).

        Each entry reports name, kind, CRS, feature count / raster size,
        WGS84 extent, source, age and expiry. ``filter`` keeps only datasets
        whose name, note, CRS or kind contains the substring.
        """
        try:
            rows = _cache().list(filter=filter)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "count": len(rows), "datasets": rows}

    def geocache_sync() -> dict:
        """Reconcile the inventory with the files on disk and delete expired
        datasets. Returns counts of added / refreshed / dropped / expired."""
        try:
            summary = _cache().sync()
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
            return _cache().note(path, note, layer=layer, ttl_days=ttl_days)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return [geocache_list, geocache_sync, geocache_note]
