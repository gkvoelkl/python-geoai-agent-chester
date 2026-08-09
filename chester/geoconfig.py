"""Reader for the ``geodata`` config block — shared by the agent and the CLIs.

``.chester/chester.json`` carries a ``geodata`` block: the read-only data roots,
the PostGIS connector, extra STAC catalogs — and the GeoCache retention settings
(``ttl_days`` / ``ttl_by_source``) plus the background-sync interval.

This lives in its own module because **both sides must read the same values**.
The agent wiring (:mod:`agent_build`) needs them, but so does ``data.py``, which
is deliberately LLM-free and cannot import SelmaKit. If they drifted, the CLI
would delete data the agent expects to keep: with ``connector/osm`` pinned to 7
days but ``data.py --prune`` assuming the 30-day default, a prune would evict
downloads mid-workflow.

Pure stdlib on purpose — importing this pulls in neither SelmaKit nor geopandas.
Every read is best-effort: a missing or malformed config yields empty defaults so
the data-layer features go inert rather than breaking startup.
"""

from __future__ import annotations

import json
from pathlib import Path

STATE_DIR = ".chester"
# SelmaKit's default config name is "selmakit.json"; Chester uses its own name.
CONFIG_NAME = "chester.json"


def _positive_number(value, *, cast=float):
    """Coerce a config value to a positive number, or ``None`` if unusable."""
    try:
        n = cast(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _ttl_map(value) -> dict[str, int]:
    """A ``{source_pattern: days}`` map, dropping anything unusable.

    Bad entries are skipped individually rather than failing the whole block —
    one typo in a retention override must not disable the data layer.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for pattern, days in value.items():
        n = _positive_number(days, cast=int)
        if isinstance(pattern, str) and pattern and n is not None:
            out[pattern] = n
    return out


def load_geodata(state_dir: str = STATE_DIR, config_name: str = CONFIG_NAME) -> dict:
    """Read the ``geodata`` block; missing/invalid config yields empty defaults.

    Returns ``roots`` / ``postgis`` / ``stac_catalogs`` / ``statistics`` (the
    Phase 5.4–5.8 settings) plus ``ttl_days`` (``None`` → the GeoCache default),
    ``ttl_by_source`` and ``sync_interval_hours`` (``0.0`` → no background sync).
    An empty PostGIS DSN counts as unconfigured, so the connector advertises
    nothing.
    """
    try:
        cfg = json.loads((Path(state_dir) / config_name).read_text())
        gd = cfg.get("geodata") or {}
    except (OSError, ValueError):
        gd = {}
    if not isinstance(gd, dict):
        gd = {}
    postgis = gd.get("postgis") or None
    if not (postgis and postgis.get("dsn")):
        postgis = None
    return {
        "roots": gd.get("roots") or [],
        "postgis": postgis,
        "stac_catalogs": gd.get("stac_catalogs") or None,
        "statistics": gd.get("statistics") or {},
        "ttl_days": _positive_number(gd.get("ttl_days"), cast=int),
        "ttl_by_source": _ttl_map(gd.get("ttl_by_source")),
        "sync_interval_hours": _positive_number(gd.get("sync_interval_hours")) or 0.0,
    }
