"""Direktbezug einer Geodatei als **rahmenneutrale** Hülle.

Phase KM, Schritt 1. `fetch_vector` lädt eine Datei von einer Adresse und legt sie im
GeoCache ab — kein OGC-Dienst, sondern der Fall „ich habe einen Link". Getrennt von
`ogctools`, weil dieses Modul sonst über die 400-Zeilen-Grenze für neue Dateien ginge
und weil der Fall sachlich ein anderer ist: Bei einem Dienst fragt man erst, was er
anbietet; bei einer Datei lädt man sie.
"""

from __future__ import annotations

from collections.abc import Callable

from chester import provenance
from chester.discoveryshared import _saveable
from chester.geofacts import mixed_geometry_note
from chester.workspace import resolve_path


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


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """Das Werkzeug für den Direktbezug, an ``workspace`` gebunden."""
    ws = workspace

    def fetch_vector(url: str, output_path: str, bbox: list[float] | None = None) -> dict:
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

            output_path = resolve_path(output_path, ws, write=True)
            headers = {"User-Agent": "Chester-geo-ai/0.1", "Accept": "*/*"}
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
                output_path,
                source="connector/download",
                tool="fetch_vector",
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
        mixed = mixed_geometry_note(geom_types)
        if mixed:
            result["mixed_geometry"] = True
            result["warning"] = mixed
        if bbox:
            result["warning"] = (
                "the features were filtered to a BBOX (a rectangle), which includes "
                "neighbouring places — for a NAMED area this is the wrong extent. "
                "Clip against the boundary from geocode(query, "
                'output_path="boundary.gpkg") with vector_clip (reproject both to the '
                "same metric CRS first) before counting/mapping. Keep the bbox result "
                "only if an explicit coordinate window was intended."
            )
        return result

    return [fetch_vector]
