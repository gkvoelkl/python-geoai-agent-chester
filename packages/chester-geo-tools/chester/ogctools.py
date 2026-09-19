"""OGC-Dienste (WFS/WMS) und Direktbezug als **rahmenneutrale** Hüllen.

Phase KM, Schritt 1. Fünf Werkzeuge: `wfs_capabilities` und `wms_capabilities` sagen,
was ein Dienst überhaupt anbietet, `wfs_features` und `fetch_wms_map` holen es,
`fetch_vector` lädt eine Datei direkt. Die Trennung von „was gibt es" und „hol es" ist
Absicht — ein Typname, den man raten muss, ist die häufigste Ursache für einen leeren
Layer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from chester import provenance
from chester.discoveryshared import _wfs_base_and_typename
from chester.geofacts import mixed_geometry_note
from chester.workspace import resolve_path


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



def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """Die 5 Werkzeuge dieser Gruppe, an ``workspace`` gebunden."""
    ws = workspace

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

            output_path = resolve_path(output_path, ws, write=True)
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
                base["bbox"] = (
                    bbox[0],
                    bbox[1],
                    bbox[2],
                    bbox[3],
                    "urn:ogc:def:crs:OGC:1.3:CRS84",
                )
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
                output_path,
                source="connector/wfs",
                tool="wfs_features",
                query={"url": url, "typename": typename, "bbox": bbox},
                crs=gdf.crs.to_string() if gdf.crs else None,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        geom_types = sorted({g.geom_type for g in gdf.geometry if g is not None})
        out = {
            "ok": True,
            "output": output_path,
            "features": len(gdf),
            "geometry_types": geom_types,
            "crs": gdf.crs.to_string() if gdf.crs else None,
        }
        mixed = mixed_geometry_note(geom_types)
        if mixed:
            out["mixed_geometry"] = True
            out["warning"] = mixed
        return out

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
                typenames.append(
                    {
                        "name": name,
                        "title": getattr(ct, "title", None),
                        "bbox": [round(v, 6) for v in bb] if bb else None,
                        "crs": [str(c) for c in (getattr(ct, "crsOptions", None) or [])][:5],
                    }
                )
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
                layers.append(
                    {
                        "name": name,
                        "title": getattr(lyr, "title", None),
                        "bbox": [round(v, 6) for v in bb] if bb else None,
                    }
                )
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

            output_path = resolve_path(output_path, ws, write=True)
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
                return {
                    "ok": False,
                    "error": f"layer {layer!r} not offered. Available: {names}",
                }

            west, south, east, north = bbox
            if east <= west or north <= south:
                return {"ok": False, "error": "bbox must be [west, south, east, north]"}
            height = max(1, round(width * (north - south) / (east - west)))
            fmts = list(getattr(wms.getOperationByName("GetMap"), "formatOptions", []))
            fmt = next(
                (f for f in ("image/tiff", "image/geotiff", "image/png") if f in fmts),
                fmts[0] if fmts else "image/png",
            )
            # CRS:84 is always lon/lat — sidesteps WMS 1.3.0's swapped
            # EPSG:4326 axis order; fall back to EPSG:4326 for old servers.
            img, crs_err = None, None
            for srs in ("CRS:84", "EPSG:4326"):
                try:
                    img = wms.getmap(
                        layers=[layer],
                        srs=srs,
                        bbox=tuple(bbox),
                        size=(width, height),
                        format=fmt,
                        transparent=True,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    crs_err = exc
            if img is None:
                return {"ok": False, "error": f"GetMap failed: {crs_err}"}

            data = img.read()
            transform = transform_from_bounds(west, south, east, north, width, height)
            # GDAL decodes the returned PNG/TIFF bytes; re-write georeferenced.
            with MemoryFile(data) as mem, mem.open() as src:
                bands = src.read()
            with rasterio.open(
                output_path,
                "w",
                driver="GTiff",
                height=bands.shape[1],
                width=bands.shape[2],
                count=bands.shape[0],
                dtype=bands.dtype,
                crs="EPSG:4326",
                transform=transform,
            ) as dst:
                dst.write(bands)
            provenance.write_meta(
                output_path,
                source="connector/wms",
                tool="fetch_wms_map",
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


    return [wfs_features, wfs_capabilities, wms_capabilities, fetch_wms_map]
