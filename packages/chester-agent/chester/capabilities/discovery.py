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
    filetools,
    geocodetools,
    ogctools,
    osmtools,
    pointcloudtools,
    stactools,
)
from chester.qgis_env import qgis_disabled
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






_INSTRUCTIONS = """\
## Data discovery

**Pick the country-correct connector.** Chester's authoritative connectors are
country-specific (DE / CH / AT). For a DACH task, call `region_profile(bbox_or_point)`
first — it detects the country and returns the right metric **CRS** and the
authoritative **connector per data type** (terrain / boundaries / buildings / transit),
or the global fallbacks (`fetch_dem`, OSM) outside DE/CH/AT. E.g. terrain: DE →
`fetch_dgm1`, CH → `fetch_swissalti3d`, AT → `fetch_austria_dem`, else `fetch_dem`.
Germany is the primary area; CH/AT connectors reject out-of-extent bboxes anyway.

Most tasks start by turning a place/time into data:
- `geocode("Ahrweiler")` → bounding box [west, south, east, north], boundary and
  `area_km2`. Use its bbox to drive the next two tools. If the result is
  `ambiguous` (a `candidates` list is returned), confirm the `display_name`/
  `area_km2` is the place meant before continuing — re-query with region/country
  (e.g. "Neustadt, Rhineland-Palatinate") if the top hit is wrong. A wildly large
  or tiny `area_km2` is a sign the match is off.
- `osm_features(tags={"building": true}, bbox=[...])` or `place="Bonn"` → download
  OSM vectors (buildings: {"building": true}; roads: {"highway": true}). Output is
  WGS84 (EPSG:4326); reproject to a metric CRS before measuring. To select by an
  attribute, pass `where={"addr:street": "Hollerweg"}`; it filters during
  download, so you get just those features in one call (no inspect-then-filter).
- **Named area → clip to the boundary, never work off the bare bbox.** A bbox is a
  rectangle and pulls in neighbouring places. This applies to **any** task scoped to
  a named area — not only aggregates (count/area/length) but equally **selecting**
  features, **buffers / catchment zones**, and maps *of* that area. So "schools in
  Regensburg", "500 m buffer around schools in Regensburg", "parks in Bonn" all need
  the boundary, not the bbox:
  either (a) `place="Regensburg, Bayern, Deutschland"` — osmnx clips to the admin
  polygon during download (**prefer this**); or (b) for a big/slow area, download by
  bbox then `vector_clip(features, boundary)` against the polygon from
  `geocode(query, output_path="boundary.gpkg")` (reproject both to the same metric
  CRS first), then work on the clipped layer. Note: a bare `geocode` returns
  `boundary: null` — you must pass `output_path` to get the polygon file. Only use a
  raw bbox when no named area is meant (e.g. an explicit coordinate window).
  **Enclaves (Insel-Lage):** many German Landkreise are a ring around a
  *kreisfreie Stadt* that does NOT belong to the Kreis — the geocoded boundary is
  a polygon with a hole there. Both `place=` and `vector_clip` honour that hole and
  drop the enclave automatically; a bbox does not. So e.g. "buildings in Landkreis
  Regensburg" must exclude the city of Regensburg — clip, don't bbox.
- `stac_search(bbox, datetime="2021-07-01/2021-07-31", max_cloud=10)` → list
  matching satellite scenes (does NOT download pixels; returns ids + asset URLs).
- `fetch_dem(bbox, output_path)` → download Copernicus GLO-30 (~30 m) elevation
  for the area when a task needs terrain (slope/aspect/hillshade) and no DEM was
  given. Output is EPSG:4326 (degrees) — reproject to a metric CRS before any
  slope/area step.
- `fetch_dgm1(bbox, output_path)` → the **1 m** sibling of `fetch_dem`: the
  Bundesländer's open DGM1 (Bayern/NRW/Brandenburg/M-V), in a **metric** CRS
  (EPSG:25832/25833), so slope/area work directly. **Prefer it over `fetch_dem` for
  fine terrain** (detailed slope/flood, or the DTM half of a DSM−DTM building height)
  in Germany; fall back to `fetch_dem` outside the wired states.
- `fetch_dop(bbox, output_path)` → open **aerial orthophoto** (DOP), the imagery
  sibling of `fetch_dgm1`: NRW at 10 cm, Brandenburg/M-V/Bayern at 20 cm, in a metric
  CRS. This is image **data**, not a rendered picture — so unlike `fetch_wms_map` it
  may be analysed: outside Bayern band 4 is near infrared, so `spectral_index` computes
  NDVI at 10-20 cm (vegetation/tree crowns/sealed surface per parcel). Check `has_nir`
  in the result — Bayern is RGB only. Also the right backdrop for a map, a visual check
  or 3D ground texture. Tiles are 18-83 MB, so keep the bbox small (a few km); outside
  the wired states fall back to `fetch_wms_map` (picture only).
- `fetch_swissalti3d(bbox, output_path, resolution=2)` → the **Swiss** high-res DTM
  (swissALTI3D, 2 m or 0.5 m) in **EPSG:2056**; the Switzerland counterpart of
  `fetch_dgm1`. Use for fine terrain in Switzerland.
- `fetch_austria_dem(bbox, output_path)` → the **Austrian** 1 m terrain (BEV ALS DGM)
  in **EPSG:3035**; the Austria counterpart of `fetch_dgm1` / `fetch_swissalti3d`. Use
  for fine terrain in Austria (nodata −9999).
- `fetch_swisstlmregio(theme, output_path, bbox=…)` → **Swiss** topographic vector
  (swissTLMRegio) in **EPSG:2056**: `theme` ∈ roads / railways / buildings /
  landcover / lakes / rivers / builtup / poi / names. The authoritative-Swiss
  counterpart to an OSM pull; pass a `bbox` (some layers are national). The
  full-resolution swissTLM3D has no per-bbox route — use `osm_features` for finer
  Swiss detail. Switzerland only.
- `stac_search(..., catalog=…)` searches one of several catalogs — "earth-search"
  (default), "planetary-computer" (Landsat/Sentinel-1/WorldCover; its asset URLs
  are signed automatically by `fetch_raster`), or "cdse". Use it to reach data not
  on Earth Search.
- `wfs_features(url, typename, output_path, bbox=…)` → pull vector features from an
  OGC WFS service (how authoritative German/EU data is published). Needs the
  service URL and a feature `typename` from its capabilities.
- `wfs_capabilities(url)` → list a WFS service's feature types (typenames) with
  title/bbox/CRS, so you can pick the right one for `wfs_features` instead of
  guessing. Use it whenever you have a WFS URL but not the exact typename.
- `fetch_vector(url, output_path, bbox=…)` → download a *direct* vector file
  (GeoJSON/GML/zipped Shapefile/GeoPackage) from an open-data portal into the
  cache. For a WFS *service* endpoint use `wfs_features`; this is for plain file
  links.
- **WMS = pictures, not data.** `wms_capabilities(url)` lists a WMS service's
  layers; a WMS serves **rendered map images** (official basemaps, cadastre,
  zoning plans), so use it for *display only*: overlay it live via
  `render_map(wms_url=…, wms_layer=…)`{_QGIS_WMS}, or snapshot a bbox
  as a georeferenced GeoTIFF with `fetch_wms_map(url, layer, bbox, out.tif)`.
  Never analyse WMS pixels (they are colours) — for features use `wfs_features`,
  for measurable rasters use STAC/`fetch_dem`.
- `geodata_search("Stadtbezirke Regensburg")` → find authoritative datasets in
  open-data catalogs (CKAN; default the EU aggregator "data.europa.eu"). It returns
  candidates whose resources are classified by real service type — a WFS resource
  gives `wfs_url`+`typename` for `wfs_features`, a direct file for `fetch_vector`.
  Search → pick candidate → fetch → reproject → clip.
  **For an area below the Gemeinde — Stadtbezirk, Stadtteil, Ortsteil, Quartier,
  statistischer Bezirk — this is the first step, not a fallback.** BKG
  (`fetch_boundaries`) stops at `GEM`, and a geocoder answers a district name with
  an address inside it, so neither can produce that polygon. Going to OSM instead
  returns something that merely sounds right (a world-heritage outline for
  "Innenstadt", a neighbourhood point for "Zentrum") and every count made inside it
  is wrong. Same for official thematic data (Baumkataster, Lärmkarte, Schulbezirke):
  the catalog first.
- `pointcloud_search(bbox)` → list LiDAR point cloud datasets covering an area
  (OpenTopography). Then `fetch_pointcloud(bbox, tile_index_url)` downloads the
  intersecting LAZ tiles (the tile-index URL comes from the dataset's page) — these
  feed the `lidar-ground` skill.
- `pointcloud_to_copc(input_path)` → convert a LAS/LAZ point cloud to
  **COPC**{_QGIS_COPC}. Use for a
  `fetch_pointcloud` tile or a Bavarian `Laserpunktwolke` LAZ (open at
  geodaten.bayern.de/opengeodata, but downloaded via its portal — no clean per-tile URL).

**Name the source and its licence.** When a tool result carries a `licence` field,
say where the data came from and under which licence in your final answer — one
sentence is enough ("Quelle: BKG Verwaltungsgebiete, DL-DE/BY 2.0"). These are open
*attribution* licences: DL-DE/BY, CC-BY, © swisstopo. Using the data without naming
it is not a style question. You already have the wording — it is in the tool return,
you do not have to look it up.\
"""










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
            # Ohne QGIS fallen die Desktop-Sätze weg statt zu versprechen, was
            # nicht da ist (2026-09-07: 29 solche Nennungen im Prompt).
            live = not qgis_disabled()
            return (_INSTRUCTIONS
                    .replace("{_QGIS_WMS}", " or `qgis_show_wms`" if live else "")
                    .replace("{_QGIS_COPC}",
                             (", needed before `qgis_show_pointcloud` (this QGIS "
                              "loads COPC/EPT, not plain LAZ)") if live else ""))

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
