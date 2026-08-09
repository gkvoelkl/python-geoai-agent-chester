"""Country/CRS-aware region layer — pick the right connector + CRS by area (§5.10).

Chester's authoritative connectors are country-specific (DE = BKG/LoD2/DGM1 in
EPSG:25832/33 · CH = swisstopo in EPSG:2056 · AT = Statistik Austria / BEV / Vienna in
EPSG:31287/3035/31256). This cross-cutting helper answers "for *this* area, which
connector and which metric CRS?" so the agent stops guessing. Germany stays the
**primary** area; CH/AT are additive and inert outside their extent (a point elsewhere
returns country ``None`` → the global fallbacks: `fetch_dem`, OSM, geocoding).

Detection is an **offline point-in-polygon** test against simplified DE/CH/AT outlines
(`chester/resources/dach_countries.geojson`, ~0.03° / a few km, OSM/ODbL via osmnx) —
accurate enough to disambiguate the DACH border region where bounding boxes overlap
(München → DE, not AT). Pure, no SelmaKit dep.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_GEOJSON = Path(__file__).with_name("resources") / "dach_countries.geojson"

# Per-country profile: the metric CRS and the authoritative connector tool per data type.
PROFILES: dict[str, dict] = {
    "DE": {
        "country": "DE",
        "name": "Germany",
        "crs": 25832,  # UTM32N west / 25833 east — see metric_crs()
        "crs_note": "EPSG:25832 (UTM32N) west of 12°E, EPSG:25833 (UTM33N) east",
        "connectors": {
            "terrain": "fetch_dgm1",
            "boundaries": "fetch_boundaries",
            "buildings": "fetch_cityjson",
            "transit": "gtfs_feeds (de_*)",
        },
        "primary": True,
    },
    "CH": {
        "country": "CH",
        "name": "Switzerland",
        "crs": 2056,
        "crs_note": "EPSG:2056 (LV95)",
        "connectors": {
            "terrain": "fetch_swissalti3d",
            "boundaries": "fetch_swiss_boundaries",
            "buildings": "fetch_swissbuildings3d",
            "topographic_vector": "fetch_swisstlmregio",
            "transit": "gtfs_feeds (ch_*)",
        },
        "primary": False,
    },
    "AT": {
        "country": "AT",
        "name": "Austria",
        "crs": 31287,
        "crs_note": "EPSG:31287 (MGI/Austria Lambert); terrain in 3035, Vienna 3D in 31256",
        "connectors": {
            "terrain": "fetch_austria_dem",
            "boundaries": "fetch_austria_boundaries",
            "buildings": "fetch_vienna_buildings (Vienna only)",
            "transit": "gtfs_feeds (at_* — gated)",
        },
        "primary": False,
    },
}

# The global fallbacks when a point is outside DACH.
_FALLBACK = {
    "country": None,
    "name": "outside DACH",
    "crs": 4326,
    "crs_note": "no national connector — use global fallbacks",
    "connectors": {"terrain": "fetch_dem", "vector": "osm_features",
                   "discovery": "geocode / stac_search"},
    "primary": False,
}


@lru_cache(maxsize=1)
def _shapes():
    """Load the simplified DE/CH/AT outlines as shapely geometries (memoised)."""
    from shapely.geometry import shape

    doc = json.loads(_GEOJSON.read_text(encoding="utf-8"))
    return [(f["properties"]["country"], shape(f["geometry"])) for f in doc["features"]]


def detect_country(lon: float, lat: float) -> str | None:
    """Country (``DE`` / ``CH`` / ``AT``) containing the WGS84 point, else ``None``."""
    from shapely.geometry import Point

    pt = Point(lon, lat)
    for code, geom in _shapes():
        if geom.contains(pt):
            return code
    return None


def metric_crs(country: str | None, lon: float | None = None) -> int:
    """The recommended metric EPSG for a country (DE splits UTM32/33 at 12°E)."""
    if country == "DE":
        return 25833 if (lon is not None and lon >= 12.0) else 25832
    prof = PROFILES.get(country or "")
    return prof["crs"] if prof else 4326


def _center(bbox_or_point: list[float]) -> tuple[float, float]:
    """(lon, lat) centre of a [lon, lat] point or a [w, s, e, n] bbox."""
    v = list(bbox_or_point)
    if len(v) == 2:
        return v[0], v[1]
    if len(v) == 4:
        return (v[0] + v[2]) / 2.0, (v[1] + v[3]) / 2.0
    raise ValueError("expected [lon, lat] or [west, south, east, north]")


def region_profile(bbox_or_point: list[float]) -> dict:
    """Profile for a WGS84 location: detected country + metric CRS + connectors.

    ``bbox_or_point`` is ``[lon, lat]`` or ``[west, south, east, north]`` (WGS84).
    Returns the country's connector recommendations and the right metric CRS (for the
    centre), or the global fallbacks when the point is outside DE/CH/AT.
    """
    lon, lat = _center(bbox_or_point)
    code = detect_country(lon, lat)
    prof = dict(PROFILES.get(code, _FALLBACK))
    prof["detected_country"] = code
    prof["recommended_crs"] = metric_crs(code, lon)
    prof["centre"] = [round(lon, 6), round(lat, 6)]
    return prof
