"""Geokodierung und Gebietsprofil als **rahmenneutrale** Hüllen.

Phase KM, Schritt 1. `geocode` übersetzt einen Namen in Koordinaten und Ausdehnung —
mit den Warnungen, die dieses Projekt teuer gelernt hat: Passt die gefundene Fläche
nicht zur Art des Treffers, sagt es das, und für ein benanntes Gebiet weist es auf die
**amtliche** Grenze statt auf die Nominatim-Hülle hin. `region_profile` beantwortet,
welche Beschaffungswege es für eine Stelle überhaupt gibt.
"""

from __future__ import annotations

from collections.abc import Callable

from chester import provenance
from chester.discoveryshared import _OSM_LICENCE
from chester.workspace import resolve_path

_MIN_AREA_KM2 = 0.05

_PHOTON_URL = "https://photon.komoot.io/api/"

def _photon_bbox(extent: list | None) -> list | None:
    """Photon's ``extent`` is [west, north, east, south]; Chester's bbox is w,s,e,n.

    Silently passing Photon's order through would put every south edge above its
    north edge — an empty bbox that still looks like four plausible numbers.
    """
    if not extent or len(extent) != 4:
        return None
    w, n, e, s = (float(v) for v in extent)
    return [round(w, 6), round(s, 6), round(e, 6), round(n, 6)]

def _photon_lookup(query: str, limit: int = 3) -> list[dict]:
    """Full-text OSM name search as a second opinion when Nominatim finds nothing.

    Nominatim parses *addresses*; it splits "Regensburger Hauptbahnhof" into street
    and place tokens and returns nothing at all. Photon indexes OSM **names**, so it
    answers the same string with `railway=station` in Regensburg. Free, no key, same
    ODbL data — but points only, never boundary polygons, which is why this is a
    fallback and not a replacement.

    Returns [] on any failure: an unreachable second opinion must not turn a
    Nominatim miss into a crash.
    """
    try:
        import requests

        resp = requests.get(
            _PHOTON_URL,
            params={"q": query, "limit": str(max(1, limit)), "lang": "de"},
            headers={"User-Agent": "chester-geo-ai"},
            timeout=(10, 30),
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
    except Exception:  # noqa: BLE001 - the fallback is best-effort by design
        return []
    out = []
    for f in features:
        p = f.get("properties", {})
        lon, lat = f.get("geometry", {}).get("coordinates", [None, None])
        if lon is None:
            continue
        out.append({
            "display_name": ", ".join(
                x for x in (p.get("name"), p.get("city"), p.get("state"), p.get("country")) if x
            ),
            "class": p.get("osm_key"),
            "type": p.get("osm_value"),
            "centroid": [round(float(lon), 6), round(float(lat), 6)],
            "bbox": _photon_bbox(p.get("extent")),
        })
    return out

_OFFICIAL_BOUNDARY_TOOL = {
    ("Deutschland", "Germany"): "fetch_boundaries",
    ("Schweiz", "Switzerland", "Suisse", "Svizzera"): "fetch_swiss_boundaries",
    ("Österreich", "Austria"): "fetch_austria_boundaries",
}

def _official_boundary_hint(
    boundary_path: str | None, cls: str, typ: str, display_name: str
) -> str:
    """Point at the authoritative source when geocode just wrote an admin polygon.

    Chester has both routes and the prompt spends 3.6k characters saying which to
    prefer — and it is not followed. Measured across all sessions: `geocode` 131
    calls, `fetch_boundaries` 7. Twice on 2026-09-04 the agent fetched a Gemeinde
    boundary from Nominatim, once even when the user asked for the *Gemeindegrenze*
    by name.

    The gap is friction, not ignorance, so this is the second half of the fix (the
    first made `level` optional): the moment the cheap route produces an
    administrative polygon, the tool result itself names the authoritative one. Same
    device as the bbox warning, which measurably changed behaviour.

    Only fires when a polygon was actually written (`output_path` was given) for an
    administrative boundary in DACH — a courthouse, a street or a French commune
    gets nothing.
    """
    if not boundary_path or cls != "boundary" or typ != "administrative":
        return ""
    tail = (display_name or "").rsplit(",", 1)[-1].strip()
    tool = next(
        (t for countries, t in _OFFICIAL_BOUNDARY_TOOL.items() if tail in countries), ""
    )
    if not tool:
        return ""
    return (
        f"This polygon comes from OpenStreetMap. For an administrative area the "
        f"authoritative source is `{tool}` — it carries the official geometry and "
        f"the join key a statistics table needs; one call, e.g. "
        f"`{tool}(output_path=..., match=...)`. Use this OSM polygon only if the "
        f"authoritative one is unavailable, and say so if you do."
    )

def _area_match_warning(display_name: str, area_km2: float | None, cls: str, typ: str) -> str:
    """Flag a match that is a *thing* where the caller probably wanted a *place*.

    Both bad hits of 2026-08-25 look identical from the outside — `ok: true`, a
    display name containing the right words, a bbox: "Regensburger Altstadt" →
    a Regensburger Straße in **Passau**, "Regensburg Altstadt, Deutschland" →
    the **Arbeitsgericht**. What both give away is size: area_km2 was 0.0. A
    courthouse is not a district, and clipping a city analysis to one silently
    produces an answer about a building.
    """
    if area_km2 is None or area_km2 >= _MIN_AREA_KM2:
        return ""
    return (
        f"this match is a point-sized object ({area_km2} km²), not an area: "
        f"'{display_name}' [{cls}={typ}]. If you wanted a place to clip against, "
        "re-query with a more specific name, pick from `candidates`, or use the "
        "official boundary tools. Using this bbox would analyse a single address."
    )

def _bbox_area_km2(west: float, south: float, east: float, north: float) -> float:
    """Geodesic area of a [west, south, east, north] bbox in km².

    A plausibility signal: a typo that matches a whole country yields a huge
    number, a too-narrow match a tiny one — so the agent can catch a wrong
    geocode before it drives the rest of the workflow.
    """
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    area, _ = geod.polygon_area_perimeter([west, east, east, west], [south, south, north, north])
    return abs(area) / 1e6


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """Die 2 Werkzeuge dieser Gruppe, an ``workspace`` gebunden."""
    ws = workspace

    def geocode(query: str, output_path: str | None = None, candidate_limit: int = 5) -> dict:
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

        ``match_class``/``match_type`` say *what kind of thing* was matched
        (``boundary=administrative`` is a place, ``amenity=courthouse`` is a
        building). A **``warning``** appears when the hit is point-sized — the
        signature of a wrong-address match, whose bbox must not be used as a
        study area. When Nominatim finds nothing at all, a Photon name search
        answers instead (``source: "photon"``): it resolves station and
        landmark names Nominatim cannot parse, but returns no boundary.
        """
        try:
            import osmnx as ox
            from osmnx._nominatim import _download_nominatim_element

            if output_path:
                output_path = resolve_path(output_path, ws, write=True)

            try:
                elements = _download_nominatim_element(query, limit=max(1, candidate_limit))
            except Exception:  # noqa: BLE001 - no structured match
                elements = []

            if not elements:
                # Nominatim parses addresses and gives up on names it cannot
                # split; Photon indexes the names themselves. Second opinion
                # before the last-ditch point match.
                photon = _photon_lookup(query, limit=max(1, candidate_limit))
                if photon:
                    hit = photon[0]
                    res = {
                        "ok": True,
                        "query": query,
                        "source": "photon",
                        "display_name": hit["display_name"],
                        "match_class": hit["class"],
                        "match_type": hit["type"],
                        "centroid": hit["centroid"],
                        "bbox": hit["bbox"],
                        "crs": "EPSG:4326",
                        "boundary": None,
                        "note": "Nominatim found nothing; this is a Photon "
                        "name match (OSM/ODbL). Photon returns NO boundary "
                        "polygon — for a named area to clip against, use the "
                        "official boundary tools instead of this bbox.",
                    }
                    if len(photon) > 1:
                        res["ambiguous"] = True
                        res["candidates"] = photon
                    return res
                lat, lon = ox.geocode(query)  # last-ditch point match
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
                    output_path,
                    source="connector/nominatim",
                    tool="geocode",
                    query=query,
                    crs="EPSG:4326",
                    licence=_OSM_LICENCE,
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
                # class/type were computed and then dropped; they are the one
                # structured signal that separates a district from a courthouse.
                "match_class": primary["class"],
                "match_type": primary["type"],
                "boundary": boundary_path,
            }
            mismatch = _area_match_warning(
                primary["display_name"], primary["area_km2"],
                primary["class"], primary["type"],
            )
            if mismatch:
                result["warning"] = mismatch
            official = _official_boundary_hint(
                boundary_path, primary["class"], primary["type"],
                primary["display_name"],
            )
            if official:
                result["warning"] = (
                    f"{result['warning']} {official}" if result.get("warning") else official
                )
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

    return [geocode, region_profile]
