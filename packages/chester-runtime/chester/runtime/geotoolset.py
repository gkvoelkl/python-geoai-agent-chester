"""The config the wrapper-layer modules take — one reader for every agent that collects them.

Moved out of `chester.team.ressorts` (2026-10-24): chester-code collects the same tools
and must not import chester-team to get the options. Same reasoning as the move of the
wrapper layer itself into chester-geo-tools — what two siblings need lives below both.
"""

from __future__ import annotations

from typing import Any


def wrapper_options(geodata: dict) -> dict[str, dict[str, Any]]:
    """Config for the wrapper modules that take any — as `agent_build` threads it."""
    from chester.geocache import DEFAULT_TTL_DAYS

    return {
        "connectorstools": {"roots": geodata.get("roots"), "postgis": geodata.get("postgis")},
        # `ttl_days` too, not only `ttl_by_source`: without it a ressort's
        # `geocache_list` prunes on the 30-day default while the config says otherwise
        # — it would delete the user's cached data (found in review, 2026-09-20).
        "inventorytools": {"roots": geodata.get("roots"),
                           "default_ttl_days": geodata.get("ttl_days") or DEFAULT_TTL_DAYS,
                           "ttl_by_source": geodata.get("ttl_by_source")},
        "stactools": {"extra_catalogs": geodata.get("stac_catalogs")},
    }
