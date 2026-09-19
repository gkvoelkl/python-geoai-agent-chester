"""STAC-Suche und Rasterbezug als **rahmenneutrale** Hüllen.

Phase KM, Schritt 1 — dritter Schnitt aus `capabilities/discovery.py`. Drei Werkzeuge:
`stac_catalogs` findet einen passenden Katalog, `stac_search` die Szenen darin,
`fetch_raster` holt einen Ausschnitt als GeoTIFF. `_maybe_sign` gehört dazu, weil
Planetary-Computer-URLs unsigniert mit 403 antworten — ein Fehler, den man sonst für
ein Netzproblem hält.
"""

from __future__ import annotations

from collections.abc import Callable

from chester import provenance
from chester.workspace import resolve_path

_S2_ASSETS = ("red", "green", "blue", "nir", "nir08", "swir16", "scl", "visual")

_STAC_CATALOGS = {
    "earth-search": {
        "url": "https://earth-search.aws.element84.com/v1",
        "sign": False,
    },
    "planetary-computer": {
        "url": "https://planetarycomputer.microsoft.com/api/stac/v1",
        "sign": True,
    },
    "cdse": {  # Copernicus Data Space Ecosystem
        "url": "https://catalogue.dataspace.copernicus.eu/stac",
        "sign": False,
    },
}

_DEFAULT_CATALOG = "earth-search"

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


def build_tools(workspace: str,
                extra_catalogs: dict | None = None) -> list[Callable[..., dict]]:
    """Die drei STAC-Werkzeuge, an ``workspace`` gebunden.

    ``extra_catalogs`` übersteuert oder ergänzt die eingebauten Kataloge — diese
    Gruppe hängt als einzige an Zustand der Fähigkeit, nicht nur am Workspace
    (`DataDiscoveryCapability.stac_catalogs`). Ein Adapter ohne eigene Kataloge
    lässt das Argument weg.
    """
    ws = workspace
    catalogs = {**_STAC_CATALOGS, **(extra_catalogs or {})}

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
                headers={"User-Agent": "chester-geo-ai"},
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
                hits.append(
                    {
                        "title": c.get("title"),
                        "url": c.get("url"),
                        "is_api": bool(c.get("isApi")),
                        "summary": (c.get("summary") or "")[:200],
                    }
                )
        return {
            "ok": True,
            "count": len(hits),
            "catalogs": hits[:limit],
            "note": "Add a STAC *API* URL to geodata.stac_catalogs, then use "
            "stac_search(catalog=…). Non-API catalogs are browse-only.",
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
            return {
                "ok": False,
                "error": f"unknown catalog '{catalog}'; one of {sorted(catalogs)}",
            }
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
            assets = {k: it.assets[k].href for k in _S2_ASSETS if k in it.assets}
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

            output_path = resolve_path(output_path, ws, write=True)
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
                output_path,
                source="connector/stac-cog",
                tool="fetch_raster",
                query=url,
                crs=str(src.crs),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "output": output_path,
            "size": [int(data.shape[1]), int(data.shape[0])],
            "crs": str(src.crs),
        }

    return [stac_catalogs, stac_search, fetch_raster]
