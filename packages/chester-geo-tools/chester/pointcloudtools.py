"""Die drei Punktwolken-Werkzeuge als **rahmenneutrale** Hüllen.

Dritter Schnitt der Phase KM, Schritt 1 — und der erste aus
`capabilities/discovery.py`, die mit 2141 Zeilen und 21 Werkzeugen nicht in *ein*
Hüllenmodul passt. Geschnitten wird deshalb nach Themen; die Punktwolken sind der
geschlossenste Block: seine vier Helfer kommen je genau zweimal in der
Ursprungsdatei vor — Definition und eine Verwendung —, also gehören sie ganz hierher.

**Der Instruktionsblock bleibt, wo er ist.** `discovery` beschreibt alle Werkzeuge in
einem Stück; ihn je Gruppe zu zerlegen wäre redaktionelle Arbeit ohne Gegenwert — für
den MCP-Server ist er ohnehin wertlos, weil Server-Instruktionen das Modell nicht
erreichen (`internal/chester-mcp.md` §4a). Hüllenmodule brauchen nur `build_tools`.
"""

from __future__ import annotations

from collections.abc import Callable

from chester import provenance
from chester.workspace import resolve_path

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


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """Die 3 Werkzeuge dieser Gruppe, an ``workspace`` gebunden."""
    ws = workspace

    def pointcloud_search(bbox: list[float], limit: int = 10) -> dict:
        """Find LiDAR point cloud datasets covering an area (OpenTopography).

        ``bbox`` = [west, south, east, north] in WGS84. Returns the datasets
        whose coverage intersects it — name, short code, DOI/landing URL and
        time span. This is discovery only: to pull tiles, get the dataset's
        **tile index** URL (from its landing page) and call ``fetch_pointcloud``.
        """
        import json
        import urllib.request

        q = (
            f"{_OT_CATALOG}?productFormat=PointCloud&minx={bbox[0]}&miny={bbox[1]}"
            f"&maxx={bbox[2]}&maxy={bbox[3]}&detail=true&outputFormat=json"
        )
        try:
            req = urllib.request.Request(q, headers={"User-Agent": "chester-geo-ai"})
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = json.load(resp)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        out = []
        for entry in data.get("Datasets", [])[:limit]:
            d = entry.get("Dataset", entry)
            out.append(
                {
                    "name": d.get("name"),
                    "code": d.get("alternateName"),
                    "url": d.get("url"),
                    "coverage": d.get("temporalCoverage"),
                }
            )
        return {
            "ok": True,
            "count": len(out),
            "datasets": out,
            "note": "Get a dataset's tile-index URL from its landing page, "
            "then fetch_pointcloud(bbox, tile_index_url).",
        }

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

            idx_path = (
                f"/vsizip/vsicurl/{tile_index_url}"
                if tile_index_url.lower().endswith(".zip")
                else tile_index_url
            )
            gdf = gpd.read_file(idx_path)
            urls, field = _select_tile_urls(gdf, bbox, url_field)
            if field is None:
                return {
                    "ok": False,
                    "error": "could not find a download-URL column "
                    "in the tile index; pass url_field explicitly",
                    "columns": [c for c in gdf.columns if c != gdf.geometry.name],
                }
            if not urls:
                return {"ok": False, "error": "no tiles intersect the bbox"}

            import os
            import urllib.request

            out_base = resolve_path(output_dir, ws, write=True)
            os.makedirs(out_base, exist_ok=True)
            saved = []
            for u in urls[:max_tiles]:
                name = os.path.basename(u.split("?")[0])
                dest = os.path.join(out_base, name)
                urllib.request.urlretrieve(u, dest)
                provenance.write_meta(
                    dest,
                    source="connector/opentopography",
                    tool="fetch_pointcloud",
                    query={"tile_index": tile_index_url, "bbox": bbox},
                    licence=_OT_PC_LICENCE,
                )
                saved.append(dest)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "tiles": saved,
            "count": len(saved),
            "matched": len(urls),
            "url_field": field,
            "truncated": len(urls) > max_tiles,
        }

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
        out = _P(resolve_path(desired, ws, write=True))
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            _qp.QgisProcess().run(
                "pdal:createcopc", {"LAYERS": [src], "OUTPUT": str(out.parent)}
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"createcopc failed: {type(exc).__name__}: {exc}"}
        produced = out.parent / (_P(src).stem + ".copc.laz")
        if produced != out and produced.exists():
            produced.replace(out)
        if not out.exists():
            return {"ok": False, "error": "createcopc produced no COPC output"}
        provenance.write_meta(
            str(out), source="chester", tool="pointcloud_to_copc", query={"from": input_path}
        )
        return {
            "ok": True,
            "output": str(out),
            "format": "COPC",
            "note": "COPC ready — display with qgis_show_pointcloud.",
        }

    return [pointcloud_search, fetch_pointcloud, pointcloud_to_copc]
