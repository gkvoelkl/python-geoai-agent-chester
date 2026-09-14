"""Die Katalogsuche als **rahmenneutrale** Hülle.

Phase KM, Schritt 1 — zweiter Schnitt aus `capabilities/discovery.py`. `geodata_search`
durchsucht offene Datenkataloge (CKAN/DCAT) nach einem Stichwort und klassifiziert jede
gefundene Ressource nach Diensttyp, damit aus einem Treffer ein *benutzbarer* Zugang
wird und nicht nur eine Adresse.
"""

from __future__ import annotations

from collections.abc import Callable

from chester.discoveryshared import _wfs_base_and_typename

_CKAN_CATALOGS = {
    "data.europa.eu": "https://data.europa.eu/api/hub/search/ckan/package_search",
    "govdata.de": "https://www.govdata.de/ckan/api/3/action/package_search",
}

_DEFAULT_CKAN = "data.europa.eu"

_GEO_KINDS = {"WFS", "WMS", "GeoJSON", "GML", "Shapefile", "GeoPackage", "KML"}

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
        ".geojson": "GeoJSON",
        ".json": "GeoJSON",
        ".gml": "GML",
        ".zip": "Shapefile",
        ".shp": "Shapefile",
        ".gpkg": "GeoPackage",
        ".kml": "KML",
        ".csv": "CSV",
    }
    if ext in by_ext:
        return {"service": by_ext[ext], "url": url}
    fmt_map = {
        "geojson": "GeoJSON",
        "wfs": "WFS",
        "wms": "WMS",
        "shapefile": "Shapefile",
        "shp": "Shapefile",
        "gml": "GML",
        "geopackage": "GeoPackage",
        "gpkg": "GeoPackage",
        "kml": "KML",
        "csv": "CSV",
        "json": "GeoJSON",
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


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """Das Werkzeug dieser Gruppe.

    ``workspace`` bleibt ungenutzt — die Katalogsuche schreibt nichts, sie liefert
    Treffer. Die Signatur ist dieselbe wie bei den anderen Hüllenmodulen, damit ein
    Adapter alle gleich einhängen kann.
    """
    del workspace

    def geodata_search(query: str, catalog_url: str | None = None, limit: int = 10) -> dict:
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
            headers = {"User-Agent": "Chester-geo-ai/0.1", "Accept": "application/json"}
            resp = requests.get(
                endpoint or "",
                params={"q": query, "rows": str(limit)},
                headers=headers,
                timeout=(10, 60),
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
                candidates.append(
                    {
                        "title": ds.get("title"),
                        "publisher": _publisher(ds),
                        "license": _dataset_license(ds, raw),
                        "resources": resources,
                        "_geo": any(x["service"] in _GEO_KINDS for x in resources),
                    }
                )
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

    return [geodata_search]
