"""DataDiscoveryCapability — find and fetch the data a query needs.

Three discovery tools, the front door of most workflows:

* ``geocode``       — place name → bounding box + boundary geometry (osmnx/Nominatim)
* ``osm_features``  — download OSM vector features (buildings, roads, …) as GeoJSON
* ``stac_search``   — find satellite scenes by space/time/cloud (pystac-client)

osmnx handles the messy Overpass querying and geometry assembly (osm2geojson
assembles geometry for the raw-QL path); pystac-client talks to STAC catalogs.
Both are imported lazily and return ``{"ok": false,
"error": …}`` instead of raising, so a network hiccup doesn't crash the loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import (
    catalogtools,
    demtools,
    discoveryshared,
    filetools,
    geocodetools,
    ogctools,
    osmtools,
    pointcloudtools,
    stactools,
)
from chester.workspace import DEFAULT_WORKSPACE

# OpenStreetMap data (Nominatim boundaries, Overpass features) is ODbL-licensed;
# render_map must attribute it.

# Overpass mirrors tried after the primary (osmnx's configured endpoint). The
# main server is frequently saturated and 504s; a mirror fallback + retry turns
# those transient failures into a successful call. Kept short (only endpoints
# that actually respond) to bound latency when one is dead.
# Sentinel-2 asset keys worth surfacing — a short list keeps tool output small.




# OpenTopography point clouds: discovery via the catalog API (no key), download
# via per-dataset tile indexes (a shapefile/geojson of tile extents + LAZ URLs).








# Copernicus DEM GLO-30 (~30 m) — public COGs on AWS Open Data, one 1°×1° tile per
# file named by its SW integer corner. No credentials needed over HTTPS.
















# Nominatim answers a place question with whatever address it can parse, so a match
# this small is a building or a street, not an area to clip against. 0.05 km² is a
# large building; anything under it cannot be a district, let alone a town.






#: Country → the authoritative boundary tool for it. Matched against the tail of
#: Nominatim's ``display_name``, which always ends in the country.














# CKAN-style open-data catalogs for geodata_search. data.europa.eu is the EU
# aggregator (broadest coverage); others are reachable via catalog_url. Values
# are the full package_search endpoint — paths differ per catalog.

# A candidate carrying one of these geospatial kinds is ranked ahead of
# tabular/unknown-only ones.
# WFS GetFeature query params stripped when deriving the base service URL.














@dataclass
class DataDiscoveryCapability(AbstractCapability[Any]):
    """Geocoding, OSM feature download, multi-catalog STAC search, and OGC WFS."""

    workspace: str = DEFAULT_WORKSPACE
    # Extra/override STAC catalogs merged over the built-in registry (from
    # geodata.stac_catalogs). Each value is {"url": ..., "sign": bool}.
    stac_catalogs: dict | None = None

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            # The text lives with the wrapper layer (discoveryshared), where the
            # ressorts of chester-team read it too.
            return discoveryshared.instructions()

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace






















        return FunctionToolset(
            tools=[
                *osmtools.build_tools(ws),
                *ogctools.build_tools(ws),
                *filetools.build_tools(ws),
                *demtools.build_tools(ws),
                *geocodetools.build_tools(ws),
                *catalogtools.build_tools(ws),
                *pointcloudtools.build_tools(ws),
                *stactools.build_tools(ws, self.stac_catalogs),
            ]
        )
