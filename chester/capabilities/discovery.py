"""DataDiscoveryCapability — find and fetch the data a query needs.

Three discovery tools, the front door of most workflows:

* ``geocode``       — place name → bounding box + boundary geometry (osmnx/Nominatim)
* ``osm_features``  — download OSM vector features (buildings, roads, …) as GeoJSON
* ``osm_query_raw`` — run raw Overpass QL → GeoJSON (the expressiveness escape hatch)
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

from chester import provenance
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

# OpenStreetMap data (Nominatim boundaries, Overpass features) is ODbL-licensed;
# render_map must attribute it.
_OSM_LICENCE = "© OpenStreetMap contributors (ODbL)"

# Overpass mirrors tried after the primary (osmnx's configured endpoint). The
# main server is frequently saturated and 504s; a mirror fallback + retry turns
# those transient failures into a successful call. Kept short (only endpoints
# that actually respond) to bound latency when one is dead.
_OVERPASS_MIRRORS = (
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
# Server-side transient statuses worth retrying/falling back on (vs a 400 QL
# syntax error, which must fail fast — retrying a bad query is pointless).
_OVERPASS_TRANSIENT = {429, 502, 503, 504}


def _overpass_request(ql: str, headers: dict, endpoints, attempts: int = 2) -> dict:
    """POST Overpass QL, retrying transient failures and falling back to mirrors.

    Each endpoint is tried up to ``attempts`` times with a short backoff on a
    transient error (429/502/503/504, connection/read timeout); on exhaustion
    the next mirror is tried. A non-transient HTTP error (e.g. 400 for bad QL)
    raises immediately. Returns the parsed JSON, or raises the last error if
    every endpoint fails. The ``(connect, read)`` timeout bounds the wait on a
    dead mirror so the fallback stays cheap.
    """
    import time

    import requests

    last_exc: Exception | None = None
    for endpoint in endpoints:
        for attempt in range(attempts):
            try:
                resp = requests.post(
                    endpoint, data={"data": ql}, headers=headers, timeout=(10, 180)
                )
                if resp.status_code in _OVERPASS_TRANSIENT:
                    last_exc = RuntimeError(f"{endpoint} → HTTP {resp.status_code}")
                    time.sleep(2 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                time.sleep(2 * (attempt + 1))
    raise last_exc or RuntimeError("Overpass request failed")

# STAC catalog registry (doc §3.3). `sign` flags catalogs whose asset URLs must
# be signed before download — Planetary Computer hands out unsigned Azure blob
# URLs that 403 unless signed. Users add/override catalogs via geodata.stac_catalogs.
_STAC_CATALOGS = {
    "earth-search": {
        "url": "https://earth-search.aws.element84.com/v1", "sign": False,
    },
    "planetary-computer": {
        "url": "https://planetarycomputer.microsoft.com/api/stac/v1", "sign": True,
    },
    "cdse": {  # Copernicus Data Space Ecosystem
        "url": "https://catalogue.dataspace.copernicus.eu/stac", "sign": False,
    },
}
_DEFAULT_CATALOG = "earth-search"
# Sentinel-2 asset keys worth surfacing — a short list keeps tool output small.
_S2_ASSETS = ("red", "green", "blue", "nir", "nir08", "swir16", "scl", "visual")


def _read_wfs_bytes(data: bytes):
    """Parse a WFS response (GeoJSON or GML) into a GeoDataFrame.

    GeoJSON parses straight from memory; GML is written to a temp file first
    because GDAL's GML driver resolves its schema (``.gfs``) from a path.
    """
    import geopandas as gpd

    if data.lstrip()[:1] == b"{":
        from io import BytesIO

        return gpd.read_file(BytesIO(data))
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".gml", delete=False) as tf:
        tf.write(data)
        tmp = tf.name
    try:
        return gpd.read_file(tmp)
    finally:
        os.unlink(tmp)


# OpenTopography point clouds: discovery via the catalog API (no key), download
# via per-dataset tile indexes (a shapefile/geojson of tile extents + LAZ URLs).
_OT_CATALOG = "https://portal.opentopography.org/API/otCatalog"
_OT_PC_LICENCE = "OpenTopography point cloud — see each dataset's citation/DOI"


def _find_url_field(gdf) -> str | None:
    """The tile-index column holding per-tile download URLs (field name varies).

    Auto-detected so we don't hardcode a name per dataset: the column whose
    string values mostly look like http(s) URLs ending in .laz/.las/.zip.
    """
    geom = gdf.geometry.name
    for col in gdf.columns:
        if col == geom:
            continue
        s = gdf[col].astype("string").dropna()
        if not len(s):
            continue
        looks_url = s.str.contains(r"https?://", regex=True, case=False).mean()
        looks_laz = s.str.contains(r"\.(?:la[sz]|zip)\b", regex=True, case=False).mean()
        if looks_url > 0.5 and looks_laz > 0.3:
            return col
    return None


def _select_tile_urls(gdf, bbox: list[float], url_field: str | None = None):
    """(urls, field) for tile-index rows intersecting a WGS84 ``bbox``.

    Pure (no I/O) so the selection + field detection are unit-testable offline.
    The bbox is reprojected to the index CRS before the spatial test.
    """
    from shapely.geometry import box

    field = url_field or _find_url_field(gdf)
    if field is None:
        return [], None
    aoi = box(*bbox)
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        import geopandas as gpd

        aoi = gpd.GeoSeries([aoi], crs="EPSG:4326").to_crs(gdf.crs).iloc[0]
    hit = gdf[gdf.intersects(aoi)]
    urls = [u for u in hit[field].astype("string").dropna().tolist() if u]
    return urls, field


def _maybe_sign(url: str) -> str:
    """Sign a Planetary Computer asset URL (Azure blob) so download won't 403.

    Best-effort: detected by host, signed via ``planetary_computer.sign``. Any
    other URL (or a missing package) passes through unchanged.
    """
    if "blob.core.windows.net" not in url:
        return url
    try:
        import planetary_computer as pc

        return pc.sign(url)
    except Exception:  # noqa: BLE001 - signing is best-effort
        return url

# Copernicus DEM GLO-30 (~30 m) — public COGs on AWS Open Data, one 1°×1° tile per
# file named by its SW integer corner. No credentials needed over HTTPS.
_DEM_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
_DEM_LICENCE = "Copernicus DEM GLO-30 © ESA / DLR / Airbus (free, full open licence)"


def _glo30_tile_name(lat: int, lon: int) -> str:
    """GLO-30 tile id for the 1°×1° cell whose SW corner is (lat, lon)."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def _glo30_tiles(bbox: list[float]) -> list[tuple[str, str]]:
    """(url, tile_name) for every GLO-30 tile covering ``[west, south, east, north]``.

    A tile covers ``[lon, lon+1) × [lat, lat+1)``, so the covering integer
    corners run from ``floor(west/south)`` up to ``ceil(east/north) − 1``; a
    degenerate (zero-width) bbox still yields its containing tile.
    """
    import math

    west, south, east, north = bbox
    lon_lo, lon_hi = math.floor(west), max(math.ceil(east), math.floor(west) + 1)
    lat_lo, lat_hi = math.floor(south), max(math.ceil(north), math.floor(south) + 1)
    tiles = []
    for lat in range(lat_lo, lat_hi):
        for lon in range(lon_lo, lon_hi):
            name = _glo30_tile_name(lat, lon)
            tiles.append((f"{_DEM_BASE}/{name}/{name}.tif", name))
    return tiles

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
  bbox then `qgis_clip(features, boundary)` against the polygon from
  `geocode(query, output_path="boundary.gpkg")` (reproject both to the same metric
  CRS first), then work on the clipped layer. Note: a bare `geocode` returns
  `boundary: null` — you must pass `output_path` to get the polygon file. Only use a
  raw bbox when no named area is meant (e.g. an explicit coordinate window).
  **Enclaves (Insel-Lage):** many German Landkreise are a ring around a
  *kreisfreie Stadt* that does NOT belong to the Kreis — the geocoded boundary is
  a polygon with a hole there. Both `place=` and `qgis_clip` honour that hole and
  drop the enclave automatically; a bbox does not. So e.g. "buildings in Landkreis
  Regensburg" must exclude the city of Regensburg — clip, don't bbox.
- `osm_query_raw(overpass_ql, output_path)` → the escape hatch when `osm_features`
  can't express the query: regex tag values, relations, `around:<m>` radius from a
  point, boolean tag combinations. You write the Overpass QL; it must be JSON
  (`[out:json];`, prepended if missing) and emit geometry (`out geom;`) or nothing
  is saved. Output is WGS84 GeoJSON with flattened tag columns, same as
  `osm_features`. Use `osm_features` for plain tag+area downloads — this only when
  it falls short.
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
  `render_map(wms_url=…, wms_layer=…)` or `qgis_show_wms`, or snapshot a bbox
  as a georeferenced GeoTIFF with `fetch_wms_map(url, layer, bbox, out.tif)`.
  Never analyse WMS pixels (they are colours) — for features use `wfs_features`,
  for measurable rasters use STAC/`fetch_dem`.
- `geodata_search("Stadtbezirke Regensburg")` → find authoritative datasets in
  open-data catalogs (CKAN; default the EU aggregator "data.europa.eu"). **This
  is the fallback when a layer is not in OSM** (city districts, official thematic
  data): it returns candidates whose resources are classified by real service
  type — a WFS resource gives `wfs_url`+`typename` for `wfs_features`, a direct
  file for `fetch_vector`. Search → pick candidate → fetch → reproject → clip.
- `pointcloud_search(bbox)` → list LiDAR point cloud datasets covering an area
  (OpenTopography). Then `fetch_pointcloud(bbox, tile_index_url)` downloads the
  intersecting LAZ tiles (the tile-index URL comes from the dataset's page) — these
  feed the `lidar-ground` skill.
- `pointcloud_to_copc(input_path)` → convert a LAS/LAZ point cloud to **COPC**, needed
  before `qgis_show_pointcloud` (this QGIS loads COPC/EPT, not plain LAZ). Use for a
  `fetch_pointcloud` tile or a Bavarian `Laserpunktwolke` LAZ (open at
  geodaten.bayern.de/opengeodata, but downloaded via its portal — no clean per-tile URL).\
"""


def _bbox_area_km2(west: float, south: float, east: float, north: float) -> float:
    """Geodesic area of a [west, south, east, north] bbox in km².

    A plausibility signal: a typo that matches a whole country yields a huge
    number, a too-narrow match a tiny one — so the agent can catch a wrong
    geocode before it drives the rest of the workflow.
    """
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    area, _ = geod.polygon_area_perimeter(
        [west, east, east, west], [south, south, north, north]
    )
    return abs(area) / 1e6


def _saveable(gdf):
    """Prepare an OSM GeoDataFrame so any file driver accepts it.

    Two OSM quirks break naive writes:

    * Case-colliding columns — mappers spell the same tag both ways
      (``fixme``/``FIXME``), so geopandas holds two columns. GeoPackage (SQLite)
      treats identifiers case-insensitively and refuses the duplicate. Each
      case-insensitive group is merged into one column (first-seen name wins;
      later columns fill only where the winner is null), so GPKG/Shapefile/…
      accept it — GeoJSON tolerated the pair, other drivers don't.
    * List/dict-valued cells — some tags parse to lists; those are joined to
      strings the driver can store.
    """
    out = gdf.copy()
    geom = out.geometry.name

    groups: dict[str, list[str]] = {}
    for col in out.columns:
        if col == geom:
            continue
        groups.setdefault(col.casefold(), []).append(col)
    for cols in groups.values():
        if len(cols) == 1:
            continue
        keep, *rest = cols
        for other in rest:
            missing = out[keep].isna()
            out.loc[missing, keep] = out.loc[missing, other]
        out = out.drop(columns=rest)

    for col in out.columns:
        if col == geom:
            continue
        if out[col].map(lambda v: isinstance(v, (list, set, dict))).any():
            out[col] = out[col].map(
                lambda v: ";".join(map(str, v)) if isinstance(v, (list, set)) else str(v)
            )
    return out


def _stringify_tag_value(value):
    """Coerce an OSM tag value into what osmnx accepts (bool / str / list of str).

    The model naturally writes numeric OSM values as numbers — ``admin_level: 8``,
    ``layer: -1`` — but osmnx rejects anything that isn't a bool, str, or list of
    str. Turn ints/floats into their string form (``8`` → ``"8"``; a whole float
    ``8.0`` → ``"8"``) so a common, correct query isn't a type error.
    """
    if isinstance(value, bool):  # bool is an int subclass — keep it as a bool
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, list):
        return [_stringify_tag_value(v) for v in value]
    return value


def _stringify_tags(tags: dict) -> dict:
    """Apply :func:`_stringify_tag_value` to every tag value."""
    return {k: _stringify_tag_value(v) for k, v in tags.items()}


def _apply_where(gdf, where: dict):
    """Keep rows matching every column→value pair in ``where`` (case-insensitive,
    exact match on the stringified value).

    Returns ``(filtered_gdf, missing_columns)``; a missing column is reported
    rather than silently matching nothing.
    """
    missing = [c for c in where if c not in gdf.columns]
    if missing:
        return gdf, missing
    mask = None
    for col, value in where.items():
        col_norm = gdf[col].astype("string").str.strip().str.casefold()
        # fillna(False): null cells must not poison the boolean mask with <NA>.
        m = (col_norm == str(value).strip().casefold()).fillna(False)
        mask = m if mask is None else (mask & m)
    return gdf[mask], []


# CKAN-style open-data catalogs for geodata_search. data.europa.eu is the EU
# aggregator (broadest coverage); others are reachable via catalog_url. Values
# are the full package_search endpoint — paths differ per catalog.
_CKAN_CATALOGS = {
    "data.europa.eu": "https://data.europa.eu/api/hub/search/ckan/package_search",
    "govdata.de": "https://www.govdata.de/ckan/api/3/action/package_search",
}
_DEFAULT_CKAN = "data.europa.eu"

# A candidate carrying one of these geospatial kinds is ranked ahead of
# tabular/unknown-only ones.
_GEO_KINDS = {"WFS", "WMS", "GeoJSON", "GML", "Shapefile", "GeoPackage", "KML"}
# WFS GetFeature query params stripped when deriving the base service URL.
_WFS_REQ_PARAMS = {
    "service", "version", "request", "typename", "typenames", "outputformat",
    "srsname", "bbox", "maxfeatures", "count", "resulttype", "propertyname",
}


def _wfs_base_and_typename(url: str) -> tuple[str, str | None]:
    """Split a WFS GetFeature URL into its base service URL and typename.

    Catalogs often list a ready-made GetFeature request; wfs_features /
    wfs_capabilities want the plain service endpoint (keeping e.g. MapServer's
    ``map=`` param) plus the typename separately.
    """
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    typename, keep = None, []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k.lower() in ("typename", "typenames"):
            typename = v
        if k.lower() in _WFS_REQ_PARAMS:
            continue
        keep.append((k, v))
    return urlunparse(parsed._replace(query=urlencode(keep))), typename


def _classify_resource(url: str, fmt: str | None) -> dict:
    """Classify a catalog resource by its *real* service type from the URL.

    CKAN's ``format`` field is unreliable (the Regensburg WFS is tagged "CSV"),
    so inspect the URL first — ``SERVICE=WFS/WMS`` wins over any label, then the
    file extension, then the format string as a last hint. A WFS resource also
    carries its base ``wfs_url`` + ``typename``, ready for wfs_features.
    """
    import os
    from urllib.parse import urlparse

    u = (url or "").lower()
    if "service=wfs" in u:
        base, typename = _wfs_base_and_typename(url)
        return {"service": "WFS", "url": url, "wfs_url": base, "typename": typename}
    if "service=wms" in u:
        return {"service": "WMS", "url": url}
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    by_ext = {
        ".geojson": "GeoJSON", ".json": "GeoJSON", ".gml": "GML",
        ".zip": "Shapefile", ".shp": "Shapefile", ".gpkg": "GeoPackage",
        ".kml": "KML", ".csv": "CSV",
    }
    if ext in by_ext:
        return {"service": by_ext[ext], "url": url}
    fmt_map = {
        "geojson": "GeoJSON", "wfs": "WFS", "wms": "WMS", "shapefile": "Shapefile",
        "shp": "Shapefile", "gml": "GML", "geopackage": "GeoPackage",
        "gpkg": "GeoPackage", "kml": "KML", "csv": "CSV", "json": "GeoJSON",
    }
    f = (fmt or "").lower()
    for key, kind in fmt_map.items():
        if key in f:
            return {"service": kind, "url": url}
    return {"service": fmt or "unknown", "url": url}


def _resource_url(res: dict) -> str | None:
    """A CKAN resource's download URL, across catalog dialects.

    govdata.de uses ``url``; data.europa.eu uses ``access_url`` (wrapped in
    ``[...]``). Try the known keys and unwrap.
    """
    for k in ("url", "access_url", "download_url"):
        v = res.get(k)
        if v:
            return str(v).strip().strip("[]").strip()
    return None


def _publisher(ds: dict) -> str | None:
    """Dataset publisher; data.europa.eu returns the title as a language map."""
    title = (ds.get("organization") or {}).get("title")
    if isinstance(title, dict):
        return title.get("en") or title.get("de") or next(iter(title.values()), None)
    return title


def _dataset_license(ds: dict, resources: list) -> str | None:
    """Best-available licence: dataset-level, else the first resource-level one."""
    lic = ds.get("license_title") or ds.get("license_id")
    if not lic:
        for r in resources:
            if r.get("license"):
                return str(r["license"])
    return lic


def _vector_suffix(url: str, content_type: str) -> str:
    """Pick a temp-file suffix so GDAL/pyogrio selects the right driver.

    Direct catalog resources often have a telling extension; a service URL
    (e.g. a WFS GetFeature) has none, so fall back to the Content-Type header.
    """
    import os
    from urllib.parse import urlparse

    ext = os.path.splitext(urlparse(url).path)[1].lower()
    known = {".geojson", ".json", ".gml", ".zip", ".gpkg", ".kml", ".gpx"}
    if ext in known:
        return ".geojson" if ext == ".json" else ext
    ct = content_type.lower()
    if "json" in ct:
        return ".geojson"
    if "gml" in ct or "xml" in ct:
        return ".gml"
    if "zip" in ct:
        return ".zip"
    if "gpkg" in ct or "geopackage" in ct:
        return ".gpkg"
    if "kml" in ct:
        return ".kml"
    return ".geojson"  # best-effort default


@dataclass
class DataDiscoveryCapability(AbstractCapability[Any]):
    """Geocoding, OSM feature download, multi-catalog STAC search, and OGC WFS."""

    workspace: str = DEFAULT_WORKSPACE
    # Extra/override STAC catalogs merged over the built-in registry (from
    # geodata.stac_catalogs). Each value is {"url": ..., "sign": bool}.
    stac_catalogs: dict | None = None

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace
        catalogs = {**_STAC_CATALOGS, **(self.stac_catalogs or {})}

        def geocode(
            query: str, output_path: str | None = None, candidate_limit: int = 5
        ) -> dict:
            """Resolve a place name to a bounding box and boundary geometry.

            Returns bbox as [west, south, east, north] (WGS84), the centroid as
            [lon, lat] (x,y order, same convention as the bbox — NOT lat,lon), the
            matched display name, and ``area_km2`` (a plausibility check on the
            match size). When the name is ambiguous (several places match), the
            top hit is used and the alternatives are returned under
            ``candidates`` with ``ambiguous: true`` — sanity-check the display
            name and area, and if wrong, re-query with a more specific name (add
            region/country). Optionally saves the boundary polygon of the top hit
            to output_path. Feed the bbox into osm_features or stac_search.
            """
            try:
                import osmnx as ox
                from osmnx._nominatim import _download_nominatim_element

                if output_path:
                    output_path = resolve_path(output_path, ws)

                try:
                    elements = _download_nominatim_element(
                        query, limit=max(1, candidate_limit)
                    )
                except Exception:  # noqa: BLE001 - no structured match
                    elements = []

                if not elements:  # last-ditch point match (no boundary/bbox)
                    lat, lon = ox.geocode(query)
                    return {
                        "ok": True,
                        "query": query,
                        "centroid": [round(lon, 6), round(lat, 6)],
                        "bbox": None,
                        "note": "only a point match was found (no boundary)",
                    }

                def _candidate(elem: dict) -> dict:
                    # Nominatim boundingbox is [south, north, west, east] strings.
                    s, n, w, e = (float(v) for v in elem["boundingbox"])
                    bbox = [round(w, 6), round(s, 6), round(e, 6), round(n, 6)]
                    return {
                        "display_name": elem.get("display_name") or elem.get("name"),
                        "class": elem.get("class"),
                        "type": elem.get("type"),
                        "bbox": bbox,
                        "area_km2": round(_bbox_area_km2(*bbox), 1),
                        "importance": round(elem.get("importance", 0.0), 3),
                    }

                candidates = [_candidate(e) for e in elements]
                top, primary = elements[0], candidates[0]

                # Save the top hit's boundary polygon if requested and polygonal.
                from shapely.geometry import shape

                boundary_path = None
                geojson = top.get("geojson")
                geom = shape(geojson) if geojson else None
                if (
                    output_path
                    and geom is not None
                    and geom.geom_type in ("Polygon", "MultiPolygon")
                ):
                    import geopandas as gpd

                    gpd.GeoDataFrame(
                        {"display_name": [primary["display_name"]]},
                        geometry=[geom],
                        crs="EPSG:4326",
                    ).to_file(output_path)
                    provenance.write_meta(
                        output_path, source="connector/nominatim", tool="geocode",
                        query=query, crs="EPSG:4326", licence=_OSM_LICENCE,
                    )
                    boundary_path = output_path

                result = {
                    "ok": True,
                    "query": query,
                    "display_name": primary["display_name"],
                    "bbox": primary["bbox"],
                    "centroid": [round(float(top["lon"]), 6), round(float(top["lat"]), 6)],
                    "crs": "EPSG:4326",
                    "area_km2": primary["area_km2"],
                    "boundary": boundary_path,
                }
                if len(candidates) > 1:
                    result["ambiguous"] = True
                    result["candidates"] = candidates
                    result["note"] = (
                        f"{len(candidates)} places match '{query}'; using the top "
                        f"hit '{primary['display_name']}' ({primary['area_km2']} km²). "
                        "If that is the wrong place, re-query with a more specific "
                        "name (add region/country) or pick a bbox from candidates."
                    )
                return result
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        def osm_features(
            tags: dict,
            output_path: str,
            place: str | None = None,
            bbox: list[float] | None = None,
            where: dict | None = None,
            max_features: int | None = None,
        ) -> dict:
            """Download OSM features matching ``tags`` as a GeoJSON layer (WGS84).

            Provide either ``place`` (e.g. "Bonn, Germany"; clips to the admin
            boundary — prefer it for named areas) or ``bbox`` as
            [west, south, east, north]. tags examples: {"building": true} for
            buildings, {"highway": true} for roads, {"natural": "water"} for water.
            For administrative boundaries use {"boundary": "administrative",
            "admin_level": 8} — in Germany admin_level 6 = Landkreis/Kreis, 7 =
            Verwaltungsgemeinschaft, 8 = Gemeinde/Stadt. Numeric tag values (an int
            like ``admin_level: 8``) are accepted and coerced to strings.

            Optional ``where`` filters by attribute right after download, e.g.
            {"addr:street": "Hollerweg"} keeps only buildings on that street
            (case-insensitive exact match, every pair must hold). This avoids
            downloading a whole town and filtering afterwards.

            ``max_features`` defaults to None (keep every matched feature); pass
            an int only to cap a very large area for a quick map — the result's
            ``warning`` then flags that the count is incomplete.
            """
            if not place and not bbox:
                return {"ok": False, "error": "provide either place or bbox"}
            try:
                import osmnx as ox

                output_path = resolve_path(output_path, ws)
                tags = _stringify_tags(tags)  # osmnx rejects int/float tag values
                if place:
                    gdf = ox.features_from_place(place, tags=tags)
                else:
                    # Der Waechter oben hat sichergestellt, dass eines von beiden
                    # gesetzt ist; ohne place bleibt bbox.
                    assert bbox is not None
                    w, so, e, no = bbox
                    gdf = ox.features_from_bbox((w, so, e, no), tags=tags)
                if gdf.empty:
                    return {"ok": False, "error": f"no OSM features matched tags {tags}"}

                if where:
                    gdf, missing = _apply_where(gdf, where)
                    if missing:
                        return {
                            "ok": False,
                            "error": f"where references unknown column(s) {missing}",
                            "available_columns": [
                    c for c in gdf.columns if c != gdf.geometry.name
                ][:40],
                        }
                    if gdf.empty:
                        return {"ok": False, "error": f"where {where} matched 0 features"}

                matched = len(gdf)
                # max_features=None (the default) downloads everything; a caller
                # may pass an int to cap large areas for a quick map.
                truncated = max_features is not None and matched > max_features
                if truncated:
                    gdf = gdf.iloc[:max_features]
                _saveable(gdf).to_file(output_path)
                provenance.write_meta(
                    output_path, source="connector/osm", tool="osm_features",
                    query={k: v for k, v in
                           {"tags": tags, "place": place, "bbox": bbox, "where": where}.items()
                           if v is not None},
                    crs="EPSG:4326", licence=_OSM_LICENCE,
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            geom_types = sorted({g.geom_type for g in gdf.geometry if g is not None})
            result = {
                "ok": True,
                "features": len(gdf),
                "geometry_types": geom_types,
                "crs": "EPSG:4326",
                "output": output_path,
            }
            warnings: list[str] = []
            if truncated:
                warnings.append(
                    f"{matched} features matched but output was capped at "
                    f"{max_features}; narrow the area/tags or raise max_features "
                    "— the count is NOT complete."
                )
            if bbox and not place:
                warnings.append(
                    "these features come from a BBOX (a rectangle), which includes "
                    "neighbouring places — for a NAMED area (a city/Gemeinde/Kreis) this "
                    "is an overcount and the wrong extent. If the task is about a named "
                    "area, re-run with place=\"<name>\" (osmnx clips to the admin polygon), "
                    "or qgis_clip this layer against the boundary from "
                    "geocode(query, output_path=\"boundary.gpkg\"), before buffering/"
                    "counting/mapping. Only keep the bbox result if an explicit "
                    "coordinate window was intended."
                )
            if warnings:
                result["warning"] = " ".join(warnings)
            return result

        def osm_query_raw(overpass_ql: str, output_path: str) -> dict:
            """Run raw Overpass QL and save the result as a GeoJSON layer (WGS84).

            The escape hatch for queries ``osm_features`` can't express — regex
            tag matches, relations, ``around:`` radius, boolean combinations,
            ``nwr``. You write the Overpass QL yourself; Chester runs it and
            assembles the geometry (nodes/ways/relations → points/lines/polygons).

            Two requirements for the QL:
              * Output JSON — start with ``[out:json];`` (prepended if absent).
              * Emit geometry — end statements with ``out geom;`` (or the
                ``(._;>;); out;`` recursion), else ways/relations come back
                without coordinates and nothing is saved.

            Example — cafes within 500 m of a point:
                [out:json];
                node(around:500,49.0134,12.1016)[amenity=cafe];
                out geom;

            Example — waterways in a bbox, regex on the tag value:
                [out:json];
                way["waterway"~"river|stream|canal"](48.99,12.03,49.08,12.19);
                out geom;

            Prefer ``osm_features`` for a plain tag+area download; reach for this
            only when you need Overpass expressiveness it lacks.
            """
            try:
                import geopandas as gpd
                import osm2geojson
                import osmnx as ox
                from shapely.geometry import shape

                output_path = resolve_path(output_path, ws)
                ql = overpass_ql.strip()
                if "[out:" not in ql:
                    ql = "[out:json];\n" + ql
                # overpass-api.de 406s the default requests User-Agent; reuse
                # osmnx's identifying headers so we look like a normal client.
                headers = {
                    "User-Agent": ox.settings.http_user_agent,
                    "Referer": ox.settings.http_referer,
                    "Accept": "application/json",
                }
                # Primary (osmnx's endpoint) first, then mirrors — with retry —
                # so a 504 on the saturated main server doesn't fail the tool.
                endpoints = [f"{ox.settings.overpass_url}/interpreter", *_OVERPASS_MIRRORS]
                fc = osm2geojson.json2geojson(_overpass_request(ql, headers, endpoints))
                feats = fc.get("features", [])
                if not feats:
                    return {
                        "ok": False,
                        "error": "query returned 0 features "
                        "(did you emit geometry with `out geom;`?)",
                    }
                # Flatten OSM tags to columns (like osm_features), keeping the
                # osm type/id, so downstream `where`/joins/QGIS see real fields.
                rows, geoms = [], []
                for f in feats:
                    p = f.get("properties", {})
                    rows.append({"osm_type": p.get("type"), "osm_id": p.get("id"),
                                 **(p.get("tags") or {})})
                    geoms.append(shape(f["geometry"]))
                gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")
                _saveable(gdf).to_file(output_path)
                provenance.write_meta(
                    output_path, source="connector/osm", tool="osm_query_raw",
                    query={"overpass_ql": overpass_ql},
                    crs="EPSG:4326", licence=_OSM_LICENCE,
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            geom_types = sorted({g.geom_type for g in gdf.geometry if g is not None})
            return {
                "ok": True,
                "features": len(gdf),
                "geometry_types": geom_types,
                "crs": "EPSG:4326",
                "output": output_path,
            }

        def stac_search(
            bbox: list[float],
            datetime: str,
            collections: list[str] | None = None,
            max_cloud: float | None = None,
            limit: int = 10,
            catalog: str = _DEFAULT_CATALOG,
        ) -> dict:
            """Search a STAC catalog for satellite scenes (metadata only, no pixels).

            bbox is [west, south, east, north]; datetime is an ISO range like
            "2021-07-01/2021-07-31". collections defaults to ["sentinel-2-l2a"].
            max_cloud filters eo:cloud_cover (percent). ``catalog`` selects the
            source — one of the configured catalogs (default "earth-search"; also
            "planetary-computer" for Landsat/Sentinel-1/WorldCover, "cdse"). Asset
            URLs from Planetary Computer are signed automatically by fetch_raster.
            Returns scene ids, dates, cloud cover and asset (band) URLs.
            """
            cat_cfg = catalogs.get(catalog)
            if cat_cfg is None:
                return {"ok": False,
                        "error": f"unknown catalog '{catalog}'; one of {sorted(catalogs)}"}
            try:
                from pystac_client import Client

                cols = collections or ["sentinel-2-l2a"]
                query = {"eo:cloud_cover": {"lt": max_cloud}} if max_cloud is not None else None
                cat = Client.open(cat_cfg["url"])
                search = cat.search(
                    collections=cols,
                    bbox=bbox,
                    datetime=datetime,
                    query=query,
                    max_items=limit,
                )
                items = list(search.items())
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            results = []
            for it in items:
                assets = {
                    k: it.assets[k].href for k in _S2_ASSETS if k in it.assets
                }
                results.append(
                    {
                        "id": it.id,
                        "datetime": str(it.datetime),
                        "collection": it.collection_id,
                        "cloud_cover": it.properties.get("eo:cloud_cover"),
                        "assets": assets,
                    }
                )
            return {"ok": True, "catalog": catalog, "count": len(results), "items": results}

        def fetch_raster(url: str, bbox: list[float], output_path: str) -> dict:
            """Download a bbox window of a remote (COG) raster to a local GeoTIFF.

            Use this to pull a STAC asset band (from stac_search) for just the area
            of interest instead of the whole scene. ``url`` is the asset href, bbox
            is [west, south, east, north] in WGS84. Returns the local path and size.
            """
            try:
                import rasterio
                from rasterio.warp import transform_bounds
                from rasterio.windows import from_bounds

                output_path = resolve_path(output_path, ws)
                url = _maybe_sign(url)  # Planetary Computer blob URLs 403 unsigned
                # Efficient remote COG access: avoid directory listing and use
                # HTTP range requests instead of pulling the whole file.
                gdal_env = rasterio.Env(
                    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                    GDAL_HTTP_MULTIRANGE="YES",
                    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                )
                with gdal_env, rasterio.open(url) as src:
                    left, bottom, right, top = transform_bounds("EPSG:4326", src.crs, *bbox)
                    window = from_bounds(left, bottom, right, top, src.transform)
                    data = src.read(1, window=window)
                    if data.size == 0:
                        return {"ok": False, "error": "bbox does not overlap the raster"}
                    profile = src.profile
                    profile.update(
                        driver="GTiff",
                        height=data.shape[0],
                        width=data.shape[1],
                        transform=src.window_transform(window),
                    )
                    with rasterio.open(output_path, "w", **profile) as dst:
                        dst.write(data, 1)
                provenance.write_meta(
                    output_path, source="connector/stac-cog", tool="fetch_raster",
                    query=url, crs=str(src.crs),
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "output": output_path,
                "size": [int(data.shape[1]), int(data.shape[0])],
                "crs": str(src.crs),
            }

        def fetch_dem(bbox: list[float], output_path: str) -> dict:
            """Download a bbox of the Copernicus DEM GLO-30 (~30 m) as a GeoTIFF.

            Elevation for ``bbox`` = [west, south, east, north] in WGS84. Mosaics
            the public Copernicus GLO-30 tiles covering the area and writes a
            single-band GeoTIFF (in EPSG:4326, **degrees**). Reproject to a metric
            CRS before computing slope/area. Use this when a workflow needs terrain
            and no DEM was provided. Returns the local path, size and tiles used.
            """
            try:
                import rasterio
                from rasterio.merge import merge

                output_path = resolve_path(output_path, ws)
                west, south, east, north = bbox
                gdal_env = rasterio.Env(
                    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                    GDAL_HTTP_MULTIRANGE="YES",
                    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                )
                datasets, missing = [], []
                with gdal_env:
                    for url, name in _glo30_tiles(bbox):
                        try:
                            datasets.append(rasterio.open(url))
                        except Exception:  # noqa: BLE001 - ocean/absent tile: skip
                            missing.append(name)
                    if not datasets:
                        return {
                            "ok": False,
                            "error": "no Copernicus DEM tiles cover the bbox (open water?)",
                            "tiles_missing": missing,
                        }
                    try:
                        mosaic, transform = merge(datasets, bounds=(west, south, east, north))
                        crs = str(datasets[0].crs)
                        profile = datasets[0].profile
                    finally:
                        for ds in datasets:
                            ds.close()
                    profile.update(
                        driver="GTiff", count=1,
                        height=mosaic.shape[1], width=mosaic.shape[2], transform=transform,
                    )
                    with rasterio.open(output_path, "w", **profile) as dst:
                        dst.write(mosaic[0], 1)
                provenance.write_meta(
                    output_path, source="connector/copernicus-dem", tool="fetch_dem",
                    query={"bbox": bbox}, crs=crs, licence=_DEM_LICENCE,
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "output": output_path,
                "size": [int(mosaic.shape[2]), int(mosaic.shape[1])],
                "crs": crs,
                "tiles_used": len(datasets),
                "resolution_m": 30,
                "note": "GLO-30 elevation in EPSG:4326 (degrees) — reproject to a "
                "metric CRS before slope/area calculations.",
            }

        def fetch_dgm1(bbox: list[float], output_path: str,
                       state: str | None = None) -> dict:
            """Download open **1 m** terrain (DGM1) for a bbox as a GeoTIFF.

            The high-resolution sibling of ``fetch_dem``: where ``fetch_dem`` is
            Copernicus GLO-30 (~30 m, degrees), this is the Bundesländer's open
            **1 m** DGM in a **metric** CRS (EPSG:25832) — so slope/area work
            directly, and it can serve as the DTM half of a DSM−DTM building
            height. ``bbox`` = [west, south, east, north] in WGS84; the state
            auto-detects (or pass ``state`` = "BY"/"NW"/"BB"/"MV"). Wired: Bayern,
            NRW, Brandenburg, Mecklenburg-Vorpommern (the BKG's nationwide DGM1
            needs a registered token, so it is not used). Prefer this over
            ``fetch_dem`` for fine terrain wherever it is covered.
            """
            from chester import dgm1

            output_path = resolve_path(output_path, ws)
            tile_cache = str(resolve_path("_dgm1_tiles", ws))
            try:
                r = dgm1.fetch_dgm1(bbox, output_path, tile_cache, state=state)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path, source=f"connector/dgm1-{r['state'].lower()}",
                    tool="fetch_dgm1",
                    query={"bbox": bbox, "state": r["state"]},
                    crs=r.get("crs"), licence=r.get("licence"),
                )
            return r

        def fetch_dop(bbox: list[float], output_path: str,
                      state: str | None = None) -> dict:
            """Download an open aerial orthophoto (DOP) for a bbox as a GeoTIFF.

            The **imagery** sibling of ``fetch_dgm1``: the Bundesländer's open
            DOP, mosaicked over ``bbox`` = [west, south, east, north] in WGS84
            into a multi-band GeoTIFF in a metric CRS (EPSG:25832/25833). Wired:
            NRW (10 cm), Brandenburg, Mecklenburg-Vorpommern and Bayern (20 cm).
            All but Bayern are **RGBI**, so band 4 is near infrared and
            ``spectral_index`` can compute NDVI at that resolution — Bayern is RGB
            only, so check ``has_nir`` in the result before planning an NDVI step.
            ``state`` pins the source ("NW"/"BB"/"MV"/"BY") instead of
            auto-detecting.

            Unlike ``fetch_wms_map`` — a rendered picture that must never be
            analysed — this is image **data** with defined radiometry: use it for
            NDVI/classification, as an orthophoto backdrop for maps and visual
            checks, and as ground texture under 3D buildings. Tiles are large
            (18-83 MB each), so keep the bbox small.
            """
            from chester import dop

            output_path = resolve_path(output_path, ws)
            tile_cache = str(resolve_path("_dop_tiles", ws))
            try:
                r = dop.fetch_dop(bbox, output_path, tile_cache, state=state)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path, source=f"connector/dop-{r['state'].lower()}",
                    tool="fetch_dop",
                    query={"bbox": bbox, "state": r["state"]},
                    crs=r.get("crs"), licence=r.get("licence"),
                    # Aerial imagery: when the picture was taken matters as much as
                    # what it shows. Only NRW states it in the tile name.
                    acquired=r.get("acquired"),
                )
            return r

        def fetch_swissalti3d(bbox: list[float], output_path: str,
                              resolution: float = 2.0) -> dict:
            """Download the **Swiss** high-res terrain (swissALTI3D) for a bbox as a GeoTIFF.

            The Switzerland counterpart of ``fetch_dgm1``: the swisstopo open DTM
            (``resolution`` 2 m default, or 0.5 m) mosaicked over ``bbox`` = [west,
            south, east, north] in WGS84, in **EPSG:2056 (LV95, metres)** — so
            slope/area work directly. Switzerland only; for German terrain use
            ``fetch_dgm1``, elsewhere ``fetch_dem``.
            """
            from chester import swisstopo

            output_path = resolve_path(output_path, ws)
            try:
                r = swisstopo.fetch_swissalti3d(bbox, output_path, resolution=resolution)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path, source="connector/swissalti3d", tool="fetch_swissalti3d",
                    query={"bbox": bbox, "resolution": resolution},
                    crs=r.get("crs"), licence=r.get("licence"),
                )
            return r

        def region_profile(bbox_or_point: list[float]) -> dict:
            """Detect the country for a WGS84 location and recommend connector + CRS.

            ``bbox_or_point`` = [lon, lat] or [west, south, east, north] (WGS84). Returns
            the detected country (DE/CH/AT, or null outside DACH), the recommended metric
            CRS, and the authoritative connector per data type (terrain / boundaries /
            buildings / transit). Call this first for a DACH task so you use the
            country-correct connector instead of guessing.
            """
            from chester import regions

            try:
                return {"ok": True, **regions.region_profile(bbox_or_point)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        def fetch_austria_dem(bbox: list[float], output_path: str) -> dict:
            """Download the **Austrian** 1 m terrain (BEV ALS DGM) for a bbox as a GeoTIFF.

            The Austria counterpart of ``fetch_dgm1`` (DE) / ``fetch_swissalti3d`` (CH):
            window-reads the covering BEV ALS Cloud-Optimized GeoTIFF tiles over ``bbox``
            = [west, south, east, north] in WGS84 and mosaics them into one GeoTIFF in
            **EPSG:3035 (metres)** — so slope/area work directly. Austria only; nodata is
            -9999. For German terrain use ``fetch_dgm1``, Swiss ``fetch_swissalti3d``,
            elsewhere ``fetch_dem``.
            """
            from chester import austria

            output_path = resolve_path(output_path, ws)
            cache_dir = str(resolve_path("_at_dgm", ws))
            try:
                r = austria.fetch_austria_dem(bbox, output_path, cache_dir)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path, source="connector/bev-als", tool="fetch_austria_dem",
                    query={"bbox": bbox}, crs=r.get("crs"), licence=r.get("licence"),
                )
            return r

        def fetch_swisstlmregio(theme: str, output_path: str,
                                bbox: list[float] | None = None) -> dict:
            """Download **Swiss** topographic vector (swissTLMRegio) for a theme as a GeoPackage.

            ``theme`` is one of roads / railways / buildings / landcover / lakes /
            rivers / builtup / poi / names. ``bbox`` = [west, south, east, north] in
            WGS84 windows it (recommended — some layers are national and large).
            Output is EPSG:2056 (LV95). swissTLMRegio is the generalised (≈1:200 000)
            authoritative Swiss topographic model; the full-resolution swissTLM3D has
            no per-bbox route, so for finer Swiss topographic detail use
            ``osm_features``. Switzerland only.
            """
            from chester import swisstopo

            output_path = resolve_path(output_path, ws)
            cache_dir = str(resolve_path("_tlmregio", ws))
            try:
                r = swisstopo.fetch_swisstlmregio(theme, output_path, cache_dir,
                                                  bbox_wgs84=bbox)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path, source="connector/swisstlmregio",
                    tool="fetch_swisstlmregio",
                    query={"theme": theme, "bbox": bbox},
                    crs=r.get("crs"), licence=r.get("licence"),
                )
            return r

        def wfs_features(
            url: str,
            typename: str,
            output_path: str,
            bbox: list[float] | None = None,
            max_features: int = 1000,
            version: str = "2.0.0",
        ) -> dict:
            """Download vector features from an OGC WFS service as GeoJSON.

            WFS is how authoritative/national data (BKG, Geoportal.de, state
            portals) is published. ``url`` is the service endpoint, ``typename``
            the feature type (from the service's capabilities, e.g.
            "ws:verwaltungsgebiete"). ``bbox`` = [west, south, east, north] in
            WGS84 limits the area. Writes GeoJSON and returns the path + count.
            """
            try:
                from owslib.wfs import WebFeatureService

                output_path = resolve_path(output_path, ws)
                # Strip any OGC request params the caller pasted into the URL
                # (service/version/request/typename/outputformat…) so they can't
                # collide with owslib's own request; recover an embedded typename.
                service_url, embedded_typename = _wfs_base_and_typename(url)
                typename = typename or embedded_typename or ""
                wfs = WebFeatureService(url=service_url, version=version)
                base: dict[str, Any] = {
                    "typename": [typename],
                    "maxfeatures": max_features,
                }
                if bbox:
                    # CRS84 is explicitly lon/lat, sidestepping WFS 2.0 axis-order pain.
                    base["bbox"] = (bbox[0], bbox[1], bbox[2], bbox[3],
                                    "urn:ogc:def:crs:OGC:1.3:CRS84")
                # Prefer GeoJSON, but many (older, German) services only do GML.
                data, last_err = None, None
                for fmt in ("application/json", "json", None):
                    params = dict(base)
                    if fmt:
                        params["outputFormat"] = fmt
                    try:
                        raw = wfs.getfeature(**params).read()
                    except Exception as exc:  # noqa: BLE001 - try the next format
                        last_err = exc
                        continue
                    if b"ExceptionReport" in raw[:2000] or b"ServiceException" in raw[:2000]:
                        last_err = raw[:300]
                        continue
                    data = raw
                    break
                if data is None:
                    return {"ok": False, "error": f"WFS request failed: {last_err}"}
                gdf = _read_wfs_bytes(data)
                if gdf.empty:
                    return {"ok": False, "error": "WFS returned no features"}
                gdf.to_file(output_path, driver="GeoJSON")
                provenance.write_meta(
                    output_path, source="connector/wfs", tool="wfs_features",
                    query={"url": url, "typename": typename, "bbox": bbox},
                    crs=gdf.crs.to_string() if gdf.crs else None,
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            geom_types = sorted({g.geom_type for g in gdf.geometry if g is not None})
            return {
                "ok": True,
                "output": output_path,
                "features": len(gdf),
                "geometry_types": geom_types,
                "crs": gdf.crs.to_string() if gdf.crs else None,
            }

        def wfs_capabilities(url: str, version: str | None = None) -> dict:
            """List the feature types (typenames) an OGC WFS service offers.

            Closes the gap that ``wfs_features`` leaves — it needs a ``typename``
            you otherwise have to guess. Parses the service's GetCapabilities and
            returns each typename with its title, WGS84 bbox and CRS options, so
            you can pick the right layer and feed it straight into
            ``wfs_features``. ``version`` is auto-negotiated (2.0.0 → 1.1.0 →
            1.0.0) unless pinned. Metadata only; downloads nothing.
            """
            try:
                from owslib.wfs import WebFeatureService

                # Strip OGC request params a caller may have pasted in — notably a
                # WMS-style version=1.3.0 — so they don't fight owslib's own
                # version negotiation below (keeps MapServer's map= etc.).
                service_url, _ = _wfs_base_and_typename(url)
                versions = [version] if version else ["2.0.0", "1.1.0", "1.0.0"]
                wfs, used, last_err = None, None, None
                for ver in versions:
                    try:
                        wfs = WebFeatureService(url=service_url, version=ver)
                        used = ver
                        break
                    except Exception as exc:  # noqa: BLE001 - try an older version
                        last_err = exc
                if wfs is None:
                    return {"ok": False, "error": f"could not read capabilities: {last_err}"}

                typenames = []
                for name, ct in wfs.contents.items():
                    bb = getattr(ct, "boundingBoxWGS84", None)
                    typenames.append({
                        "name": name,
                        "title": getattr(ct, "title", None),
                        "bbox": [round(v, 6) for v in bb] if bb else None,
                        "crs": [str(c) for c in (getattr(ct, "crsOptions", None) or [])][:5],
                    })
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "url": service_url,
                "version": used,
                "title": getattr(wfs.identification, "title", None),
                "count": len(typenames),
                "typenames": typenames,
            }

        def wms_capabilities(url: str, version: str | None = None) -> dict:
            """List the layers an OGC WMS service offers (metadata only).

            The WMS sibling of ``wfs_capabilities``: parses GetCapabilities and
            returns each layer with its title, WGS84 bbox and image formats, so
            you can pick the right layer for ``fetch_wms_map`` /
            ``render_map(wms_url=…)`` / ``qgis_show_wms``. Remember: a WMS serves
            **rendered map images** (pictures), not analysable data — for
            features use WFS, for raster data use WCS/STAC. ``version`` is
            auto-negotiated (1.3.0 → 1.1.1) unless pinned.
            """
            try:
                from owslib.wms import WebMapService

                service_url, _ = _wfs_base_and_typename(url)
                versions = [version] if version else ["1.3.0", "1.1.1"]
                wms, used, last_err = None, None, None
                for ver in versions:
                    try:
                        wms = WebMapService(url=service_url, version=ver)
                        used = ver
                        break
                    except Exception as exc:  # noqa: BLE001 - try an older version
                        last_err = exc
                if wms is None:
                    return {"ok": False, "error": f"could not read capabilities: {last_err}"}

                layers = []
                for name, lyr in wms.contents.items():
                    bb = getattr(lyr, "boundingBoxWGS84", None)
                    layers.append({
                        "name": name,
                        "title": getattr(lyr, "title", None),
                        "bbox": [round(v, 6) for v in bb] if bb else None,
                    })
                formats = list(getattr(wms.getOperationByName("GetMap"), "formatOptions", []))[:8]
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "url": service_url,
                "version": used,
                "title": getattr(wms.identification, "title", None),
                "count": len(layers),
                "layers": layers,
                "formats": formats,
                "note": "WMS delivers rendered images, not data — use it for "
                        "display/backdrops; for features use wfs_features.",
            }

        def fetch_wms_map(
            url: str,
            layer: str,
            bbox: list[float],
            output_path: str,
            width: int = 1024,
            version: str | None = None,
        ) -> dict:
            """Fetch a WMS GetMap image of ``bbox`` as a georeferenced GeoTIFF.

            ``bbox`` = [west, south, east, north] in WGS84; ``layer`` is a layer
            name from ``wms_capabilities``. The rendered image is written as a
            GeoTIFF (EPSG:4326) into the cache, so it can back ``render_map`` /
            ``qgis_show`` like any raster. **This is a picture, not data** —
            pixel values are colours; never analyse it (no zonal stats, no
            classification). ``width`` is the image width in px (height follows
            the bbox aspect).
            """
            try:
                import rasterio
                from owslib.wms import WebMapService
                from rasterio.io import MemoryFile
                from rasterio.transform import from_bounds as transform_from_bounds

                output_path = resolve_path(output_path, ws)
                service_url, _ = _wfs_base_and_typename(url)
                versions = [version] if version else ["1.3.0", "1.1.1"]
                wms, used, last_err = None, None, None
                for ver in versions:
                    try:
                        wms = WebMapService(url=service_url, version=ver)
                        used = ver
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_err = exc
                if wms is None:
                    return {"ok": False, "error": f"could not read capabilities: {last_err}"}
                if layer not in wms.contents:
                    names = ", ".join(list(wms.contents)[:15])
                    return {"ok": False,
                            "error": f"layer {layer!r} not offered. Available: {names}"}

                west, south, east, north = bbox
                if east <= west or north <= south:
                    return {"ok": False, "error": "bbox must be [west, south, east, north]"}
                height = max(1, round(width * (north - south) / (east - west)))
                fmts = list(getattr(wms.getOperationByName("GetMap"), "formatOptions", []))
                fmt = next((f for f in ("image/tiff", "image/geotiff", "image/png")
                            if f in fmts), fmts[0] if fmts else "image/png")
                # CRS:84 is always lon/lat — sidesteps WMS 1.3.0's swapped
                # EPSG:4326 axis order; fall back to EPSG:4326 for old servers.
                img, crs_err = None, None
                for srs in ("CRS:84", "EPSG:4326"):
                    try:
                        img = wms.getmap(layers=[layer], srs=srs, bbox=tuple(bbox),
                                         size=(width, height), format=fmt,
                                         transparent=True)
                        break
                    except Exception as exc:  # noqa: BLE001
                        crs_err = exc
                if img is None:
                    return {"ok": False, "error": f"GetMap failed: {crs_err}"}

                data = img.read()
                transform = transform_from_bounds(west, south, east, north,
                                                  width, height)
                # GDAL decodes the returned PNG/TIFF bytes; re-write georeferenced.
                with MemoryFile(data) as mem, mem.open() as src:
                    bands = src.read()
                with rasterio.open(
                    output_path, "w", driver="GTiff",
                    height=bands.shape[1], width=bands.shape[2],
                    count=bands.shape[0], dtype=bands.dtype,
                    crs="EPSG:4326", transform=transform,
                ) as dst:
                    dst.write(bands)
                provenance.write_meta(
                    output_path, source="connector/wms", tool="fetch_wms_map",
                    query={"url": service_url, "layer": layer, "bbox": bbox},
                    crs="EPSG:4326",
                    licence=getattr(wms.identification, "accessconstraints", None),
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "output": output_path,
                "layer": layer,
                "version": used,
                "format": fmt,
                "size": [width, height],
                "crs": "EPSG:4326",
                "note": "rendered map image (colours, not data) — display only, "
                        "do not analyse pixel values",
            }

        def fetch_vector(
            url: str, output_path: str, bbox: list[float] | None = None
        ) -> dict:
            """Download a direct vector file (GeoJSON/GML/zipped Shapefile/GPKG).

            The companion to ``wfs_features`` for catalog resources that are a
            *file link* rather than a live WFS service (e.g. a GeoJSON or a zipped
            Shapefile from an open-data portal). Reads it, optionally keeps only
            features intersecting ``bbox`` = [west, south, east, north] in WGS84,
            and writes it into the cache. The format is inferred from the URL /
            Content-Type. For a WFS *service* endpoint use ``wfs_features``.
            """
            try:
                import os
                import tempfile

                import geopandas as gpd
                import requests
                from shapely.geometry import box

                output_path = resolve_path(output_path, ws)
                headers = {"User-Agent": "Chester-geoai/0.1", "Accept": "*/*"}
                resp = requests.get(url, headers=headers, timeout=(10, 300))
                resp.raise_for_status()
                suffix = _vector_suffix(url, resp.headers.get("Content-Type", ""))
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                    tf.write(resp.content)
                    tmp = tf.name
                try:
                    gdf = gpd.read_file(tmp)
                finally:
                    os.unlink(tmp)
                if gdf.empty:
                    return {"ok": False, "error": "resource contained no features"}

                if bbox:
                    aoi = box(*bbox)
                    if gdf.crs and gdf.crs.to_epsg() != 4326:
                        aoi = gpd.GeoSeries([aoi], crs="EPSG:4326").to_crs(gdf.crs).iloc[0]
                    gdf = gdf[gdf.intersects(aoi)]
                    if gdf.empty:
                        return {"ok": False, "error": f"no features within bbox {bbox}"}

                _saveable(gdf).to_file(output_path)
                provenance.write_meta(
                    output_path, source="connector/download", tool="fetch_vector",
                    query={"url": url, "bbox": bbox},
                    crs=gdf.crs.to_string() if gdf.crs else None,
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            geom_types = sorted({g.geom_type for g in gdf.geometry if g is not None})
            result = {
                "ok": True,
                "output": output_path,
                "features": len(gdf),
                "geometry_types": geom_types,
                "crs": gdf.crs.to_string() if gdf.crs else None,
            }
            if bbox:
                result["warning"] = (
                    "the features were filtered to a BBOX (a rectangle), which includes "
                    "neighbouring places — for a NAMED area this is the wrong extent. "
                    "Clip against the boundary from geocode(query, "
                    "output_path=\"boundary.gpkg\") with qgis_clip (reproject both to the "
                    "same metric CRS first) before counting/mapping. Keep the bbox result "
                    "only if an explicit coordinate window was intended."
                )
            return result

        def geodata_search(
            query: str, catalog_url: str | None = None, limit: int = 10
        ) -> dict:
            """Search open-data catalogs for authoritative datasets (CKAN).

            The go-to when a layer is **not in OpenStreetMap** — e.g. a city
            district / Stadtbezirk boundary, official thematic data. Queries a
            CKAN catalog and returns ranked candidates (geospatial ones first),
            each with title, publisher, licence and its resources **classified by
            real service type** (WFS/WMS/GeoJSON/Shapefile/…) read from the URL,
            not CKAN's unreliable format label.

            ``catalog_url`` defaults to the EU aggregator "data.europa.eu"
            (broadest); pass "govdata.de", another alias, or any CKAN
            package_search endpoint / base URL to target a specific portal.

            Then act on a candidate: a WFS resource carries ``wfs_url`` +
            ``typename`` → feed `wfs_features` (or `wfs_capabilities` first); a
            direct file (GeoJSON/Shapefile/…) → `fetch_vector`. Discovery only;
            downloads nothing. A null ``license`` means terms are unverified.
            """
            try:
                import requests

                endpoint = _CKAN_CATALOGS.get(catalog_url or _DEFAULT_CKAN, catalog_url)
                if endpoint and "package_search" not in endpoint:
                    endpoint = endpoint.rstrip("/") + "/api/3/action/package_search"
                headers = {"User-Agent": "Chester-geoai/0.1", "Accept": "application/json"}
                resp = requests.get(
                    endpoint or "", params={"q": query, "rows": str(limit)},
                    headers=headers, timeout=(10, 60),
                )
                resp.raise_for_status()
                result = resp.json().get("result", {})

                candidates = []
                for ds in result.get("results", []):
                    raw = ds.get("resources") or []
                    resources = [
                        _classify_resource(u, r.get("format"))
                        for r in raw
                        if (u := _resource_url(r))
                    ]
                    candidates.append({
                        "title": ds.get("title"),
                        "publisher": _publisher(ds),
                        "license": _dataset_license(ds, raw),
                        "resources": resources,
                        "_geo": any(x["service"] in _GEO_KINDS for x in resources),
                    })
                candidates.sort(key=lambda c: not c["_geo"])  # geospatial first
                for c in candidates:
                    del c["_geo"]
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "query": query,
                "catalog": endpoint,
                "total": result.get("count"),
                "count": len(candidates),
                "candidates": candidates,
                "note": (
                    "Resources are classified by URL, not CKAN's format label. "
                    "Feed a WFS resource's wfs_url+typename into wfs_features "
                    "(or wfs_capabilities); a direct file into fetch_vector. A "
                    "null license means terms are unverified — check before reuse."
                ),
            }

        def stac_catalogs(keyword: str, limit: int = 15) -> dict:
            """Discover public STAC catalogs by keyword (via stacindex.org).

            Use this to find a catalog you don't already have configured — e.g.
            "landsat", "elevation", "germany". Returns matching catalogs with
            their title, URL and whether each is a STAC *API* (queryable). To then
            search one, add its URL to ``geodata.stac_catalogs`` and pass that name
            as ``stac_search(catalog=…)``.
            """
            import json
            import urllib.request

            try:
                req = urllib.request.Request(
                    "https://stacindex.org/api/catalogs",
                    headers={"User-Agent": "chester-geoai"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    cats = json.load(resp)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            kw = keyword.lower()
            hits = []
            for c in cats:
                if c.get("isPrivate"):
                    continue
                hay = " ".join(str(c.get(k, "")) for k in ("title", "summary", "slug")).lower()
                if kw in hay:
                    hits.append({
                        "title": c.get("title"),
                        "url": c.get("url"),
                        "is_api": bool(c.get("isApi")),
                        "summary": (c.get("summary") or "")[:200],
                    })
            return {
                "ok": True,
                "count": len(hits),
                "catalogs": hits[:limit],
                "note": "Add a STAC *API* URL to geodata.stac_catalogs, then use "
                "stac_search(catalog=…). Non-API catalogs are browse-only.",
            }

        def pointcloud_search(bbox: list[float], limit: int = 10) -> dict:
            """Find LiDAR point cloud datasets covering an area (OpenTopography).

            ``bbox`` = [west, south, east, north] in WGS84. Returns the datasets
            whose coverage intersects it — name, short code, DOI/landing URL and
            time span. This is discovery only: to pull tiles, get the dataset's
            **tile index** URL (from its landing page) and call ``fetch_pointcloud``.
            """
            import json
            import urllib.request

            q = (f"{_OT_CATALOG}?productFormat=PointCloud&minx={bbox[0]}&miny={bbox[1]}"
                 f"&maxx={bbox[2]}&maxy={bbox[3]}&detail=true&outputFormat=json")
            try:
                req = urllib.request.Request(q, headers={"User-Agent": "chester-geoai"})
                with urllib.request.urlopen(req, timeout=40) as resp:
                    data = json.load(resp)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            out = []
            for entry in data.get("Datasets", [])[:limit]:
                d = entry.get("Dataset", entry)
                out.append({
                    "name": d.get("name"),
                    "code": d.get("alternateName"),
                    "url": d.get("url"),
                    "coverage": d.get("temporalCoverage"),
                })
            return {"ok": True, "count": len(out), "datasets": out,
                    "note": "Get a dataset's tile-index URL from its landing page, "
                    "then fetch_pointcloud(bbox, tile_index_url)."}

        def fetch_pointcloud(
            bbox: list[float],
            tile_index_url: str,
            output_dir: str = "pointcloud",
            max_tiles: int = 10,
            url_field: str | None = None,
        ) -> dict:
            """Download the LiDAR tiles (LAZ/LAS) intersecting a bbox via a tile index.

            ``tile_index_url`` is a dataset's tile-index file (shapefile `.zip` or
            GeoJSON of tile extents + download URLs; see `pointcloud_search`).
            Tiles intersecting ``bbox`` (WGS84) are downloaded into ``output_dir``
            in the cache (capped at ``max_tiles``); these feed the `lidar-ground`
            skill. The URL column is auto-detected unless ``url_field`` is given.
            """
            try:
                import geopandas as gpd

                idx_path = (f"/vsizip/vsicurl/{tile_index_url}"
                            if tile_index_url.lower().endswith(".zip") else tile_index_url)
                gdf = gpd.read_file(idx_path)
                urls, field = _select_tile_urls(gdf, bbox, url_field)
                if field is None:
                    return {"ok": False, "error": "could not find a download-URL column "
                            "in the tile index; pass url_field explicitly",
                            "columns": [c for c in gdf.columns if c != gdf.geometry.name]}
                if not urls:
                    return {"ok": False, "error": "no tiles intersect the bbox"}

                import os
                import urllib.request

                out_base = resolve_path(output_dir, ws)
                os.makedirs(out_base, exist_ok=True)
                saved = []
                for u in urls[:max_tiles]:
                    name = os.path.basename(u.split("?")[0])
                    dest = os.path.join(out_base, name)
                    urllib.request.urlretrieve(u, dest)
                    provenance.write_meta(
                        dest, source="connector/opentopography", tool="fetch_pointcloud",
                        query={"tile_index": tile_index_url, "bbox": bbox},
                        licence=_OT_PC_LICENCE,
                    )
                    saved.append(dest)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {"ok": True, "tiles": saved, "count": len(saved),
                    "matched": len(urls), "url_field": field,
                    "truncated": len(urls) > max_tiles}

        def pointcloud_to_copc(input_path: str, output_path: str | None = None) -> dict:
            """Convert a LAS/LAZ point cloud to **COPC** (Cloud-Optimized Point Cloud).

            The bridge to `qgis_show_pointcloud`: this QGIS build loads only COPC/EPT
            (no `pdal` provider), so a plain `.las`/`.laz` — e.g. a Bavarian
            `Laserpunktwolke` tile downloaded from geodaten.bayern.de/opengeodata, or any
            LiDAR from `fetch_pointcloud` — must be converted first. Runs PDAL's
            `pdal:createcopc` via `qgis_process` and writes `<name>.copc.laz` into the
            cache. Pass the local LAZ path (Bavaria has no clean per-tile download URL, so
            fetch the tile from its open portal first). Then `qgis_show_pointcloud` it.
            """
            from pathlib import Path as _P

            from chester import qgis_process as _qp

            src = str(resolve_path(input_path, ws))
            if not _P(src).exists():
                return {"ok": False, "error": f"no such point cloud: {input_path}"}
            desired = output_path or (_P(src).stem + ".copc.laz")
            if not desired.endswith(".copc.laz"):
                desired = _P(desired).stem + ".copc.laz"
            out = _P(resolve_path(desired, ws))
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                _qp.QgisProcess().run("pdal:createcopc",
                                      {"LAYERS": [src], "OUTPUT": str(out.parent)})
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"createcopc failed: "
                        f"{type(exc).__name__}: {exc}"}
            produced = out.parent / (_P(src).stem + ".copc.laz")
            if produced != out and produced.exists():
                produced.replace(out)
            if not out.exists():
                return {"ok": False, "error": "createcopc produced no COPC output"}
            provenance.write_meta(str(out), source="chester", tool="pointcloud_to_copc",
                                  query={"from": input_path})
            return {"ok": True, "output": str(out), "format": "COPC",
                    "note": "COPC ready — display with qgis_show_pointcloud."}

        return FunctionToolset(
            tools=[geocode, region_profile, osm_features, osm_query_raw, stac_search,
                   fetch_raster, fetch_dem, fetch_dgm1, fetch_dop, fetch_swissalti3d,
                   fetch_austria_dem, fetch_swisstlmregio, wfs_features,
                   wfs_capabilities, wms_capabilities, fetch_wms_map,
                   fetch_vector, geodata_search, stac_catalogs,
                   pointcloud_search, fetch_pointcloud, pointcloud_to_copc]
        )
