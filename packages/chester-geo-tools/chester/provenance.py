"""Provenance sidecars — a ``<file>.meta.json`` next to each Chester-written dataset.

Records *where a dataset came from* so the GeoCache can show its origin, re-fetch
a download, and attribute basemaps/OSM in rendered maps (Phase 5.2). Three
``source`` classes:

- ``connector/<name>`` — downloaded from an external source (``connector/osm``,
  ``connector/stac-cog``, ``connector/nominatim``, …).
- ``chester``          — self-created (a QGIS / GeoPandas / perception output).
- ``user``             — a referenced read-only data root; recorded in the
  inventory only, **never** given a sidecar (roots are read-only).

Division of labour with the inventory (``chester/geocache.py``): the **durable
provenance** lives here (source, query, created_at, crs, licence, tool, ttl_days),
while the **mutable lifecycle** (``last_used``) stays in the inventory, so a
touch-on-read never has to rewrite sidecars.

Writing is **best-effort**: a sidecar failure must never break the tool that
produced the data — every write swallows its errors.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

META_SUFFIX = ".meta.json"


def meta_path(path: str) -> str:
    """The sidecar path for a dataset file (``x.tif`` → ``x.tif.meta.json``)."""
    return str(path) + META_SUFFIX


def _stringify(query) -> str:
    if isinstance(query, (dict, list)):
        return json.dumps(query, sort_keys=True, default=str)
    return str(query)


def write_meta(  # noqa: PLR0913
    # Ausnahme: jedes Feld ist eine eigene Spalte des Sidecars und wird vom
    # GeoCache einzeln gelesen; ein Sammelobjekt verschoebe die Struktur nur.
    path: str,
    *,
    source: str,
    tool: str,
    query=None,
    crs: str | None = None,
    licence: str | None = None,
    ttl_days: int | None = None,
    created_at: str | None = None,
    acquired: str | None = None,
) -> None:
    """Write a provenance sidecar next to ``path`` (best-effort, never raises).

    ``source`` is one of the classes above; ``tool`` is what produced the file
    (an algorithm id or tool name); ``query`` is the request that created it
    (a place, tag dict, URL or expression) — stringified for re-fetch context.

    ``acquired`` records **when the data was captured**, as opposed to
    ``created_at`` (when Chester wrote the file). For imagery the two are years
    apart and the difference is half the answer: a 2019 orthophoto shows a
    building that was demolished in 2021. Only sources that state it can fill it.
    """
    try:
        meta: dict[str, object] = {
            "source": source,
            "tool": tool,
            "created_at": created_at
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if query is not None:
            meta["query"] = _stringify(query)
        if crs is not None:
            meta["crs"] = crs
        if licence is not None:
            meta["licence"] = licence
        if acquired is not None:
            meta["acquired"] = acquired
        if ttl_days is not None:
            meta["ttl_days"] = int(ttl_days)
        with open(meta_path(path), "w") as f:
            json.dump(meta, f, indent=2, default=str)
    except Exception:  # noqa: BLE001 - provenance is advisory; the data write won
        pass


def read_meta(path: str) -> dict | None:
    """Read the provenance sidecar for ``path``, or ``None`` if there is none."""
    try:
        with open(meta_path(path)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
