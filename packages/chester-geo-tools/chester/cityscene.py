"""What the 3D view stands on: a ground texture, a relief, a point cloud.

Split out of `citymodel.py` on 2026-10-05. The buildings alone float in a void; these
helpers supply what goes underneath — an OSM basemap tile mosaic as ground texture,
a DEM relief grid, and LAS/LAZ points coloured by classification. Each is optional and
best-effort: a missing tile server or DEM costs the backdrop, never the view.
"""

from __future__ import annotations

import re
from urllib.request import Request, urlopen

_UA = {"User-Agent": "Mozilla/5.0 (Chester Geo-AI)"}


def _deg2tile(lat: float, lon: float, z: int) -> tuple:
    """WGS84 lat/lon → fractional slippy-map tile coords at zoom ``z``."""
    import math

    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def _ground_texture_png(bbox_wgs84: list[float], max_px: int = 1400) -> bytes | None:
    """The ground plate under the 3D buildings — aerial imagery where available.

    Prefers open **DOP** over OSM tiles: under LoD2 roofs a photo shows the real
    courtyards, trees and pavement the model sits on, which is what makes the scene
    readable. One WMS GetMap, not the 18-91 MB data tiles — a texture wants a
    picture. Falls back to the OSM mosaic outside the covered states.
    """
    from chester import dop

    aerial = dop.aerial_backdrop_png(bbox_wgs84, max_px, max_px)
    return aerial or _osm_basemap_png(bbox_wgs84, max_px)


def _osm_basemap_png(bbox_wgs84: list[float], max_px: int = 1400) -> bytes | None:
    """An OSM raster (PNG bytes) covering ``bbox`` = [w,s,e,n], cropped to it.

    Mosaics the OSM tiles at a zoom that keeps the image ≤ ``max_px`` on its long
    side, then crops to the exact bbox — the ground-plate texture for the 3D viewer.
    Best-effort: returns None on any network/decoding failure (the plane is skipped).
    """
    import io
    import math

    try:
        from PIL import Image

        w, s, e, n = bbox_wgs84
        zoom = 10
        for z in range(19, 10, -1):
            x0, y0 = _deg2tile(n, w, z)   # top-left (north/west)
            x1, y1 = _deg2tile(s, e, z)   # bottom-right (south/east)
            if max((x1 - x0) * 256, (y1 - y0) * 256) <= max_px:
                zoom = z
                break
        x0, y0 = _deg2tile(n, w, zoom)
        x1, y1 = _deg2tile(s, e, zoom)
        tx0, ty0 = math.floor(x0), math.floor(y0)
        tx1, ty1 = math.ceil(x1), math.ceil(y1)
        mosaic = Image.new("RGB", ((tx1 - tx0) * 256, (ty1 - ty0) * 256))
        for tx in range(tx0, tx1):
            for ty in range(ty0, ty1):
                url = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
                with urlopen(Request(url, headers=_UA), timeout=20) as r:
                    tile = Image.open(io.BytesIO(r.read())).convert("RGB")
                mosaic.paste(tile, ((tx - tx0) * 256, (ty - ty0) * 256))
        crop = mosaic.crop((round((x0 - tx0) * 256), round((y0 - ty0) * 256),
                            round((x1 - tx0) * 256), round((y1 - ty0) * 256)))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - basemap is optional
        return None


def _dem_relief_grid(dgm_path, minx, miny, maxx, maxy, cols=96, rows=96):
    """Resample a DGM GeoTIFF to a ``cols×rows`` elevation grid over the metric bbox.

    Returns a flat row-major list (row 0 = north / max-y, matching three.js
    ``PlaneGeometry`` vertex order), with nodata replaced by the median. None on
    failure or an all-nodata window.
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.windows import from_bounds

        with rasterio.open(dgm_path) as ds:
            win = from_bounds(minx, miny, maxx, maxy, ds.transform)
            arr = ds.read(1, window=win, out_shape=(rows, cols),
                          resampling=Resampling.bilinear, boundless=True,
                          fill_value=ds.nodata if ds.nodata is not None else -9999)
            nod = ds.nodata
        arr = np.asarray(arr, dtype="float64")
        if nod is not None:
            arr[arr == nod] = np.nan
        arr[arr < -1000] = np.nan
        if np.all(np.isnan(arr)):
            return None
        arr = np.where(np.isnan(arr), np.nanmedian(arr), arr)
        return [round(float(v), 1) for v in arr.reshape(-1)]
    except Exception:  # noqa: BLE001 - relief is optional
        return None


def _fetch_relief_grid(bbox_wgs84, minx, miny, maxx, maxy, cols=96, rows=96):
    """Fetch open 1 m DGM for the area and resample it to a relief grid (best-effort)."""
    import os
    import tempfile

    from chester import dgm1

    cache = os.path.join(tempfile.gettempdir(), "chester_dgm_relief")
    os.makedirs(cache, exist_ok=True)
    tif = os.path.join(cache, "relief.tif")
    if not dgm1.fetch_dgm1(bbox_wgs84, tif, cache).get("ok"):
        return None
    grid = _dem_relief_grid(tif, minx, miny, maxx, maxy, cols, rows)
    return {"cols": cols, "rows": rows, "z": grid} if grid else None


# LAS ASPRS classification code → RGB (0..1): ground/vegetation/building/water/…
_LAS_CLASS_RGB = {
    2: (0.55, 0.40, 0.26),   # ground — brown
    3: (0.60, 0.78, 0.36),   # low vegetation
    4: (0.40, 0.68, 0.28),   # medium vegetation
    5: (0.20, 0.52, 0.20),   # high vegetation — green
    6: (0.86, 0.45, 0.24),   # building — orange
    9: (0.24, 0.52, 0.82),   # water — blue
    17: (0.65, 0.65, 0.70),  # bridge deck
}


_LAS_CLASS_DEFAULT = (0.62, 0.62, 0.66)  # unclassified / other — grey


def _classification_colors(cls):
    import numpy as np

    out = np.empty((len(cls), 3), dtype="float32")
    for i, c in enumerate(cls):
        out[i] = _LAS_CLASS_RGB.get(int(c) if c is not None else -1, _LAS_CLASS_DEFAULT)
    return out


def _pc_count(qp, pc_path: str) -> int:
    """Point count from `pdal:info` (its HTML output carries `count <N>`)."""
    import os
    import tempfile

    info = os.path.join(tempfile.mkdtemp(prefix="chester_pcinfo_"), "info.html")
    try:
        qp.run("pdal:info", {"INPUT": pc_path, "OUTPUT": info})
        m = re.search(r"count\s+(\d+)", open(info, encoding="utf-8").read())
        return int(m.group(1)) if m else 0
    except Exception:  # noqa: BLE001
        return 0


def _pointcloud_points(pc_path: str, target_epsg: int | None = None,
                       src_epsg: int | None = None, max_points: int = 150_000):
    """Decimate a LAS/LAZ/COPC to ~max_points → recentre-ready XYZ + per-point colours.

    Returns ``{"xyz": Nx3 float32, "colors": Nx3 float32, "epsg": int|None, "count": N}``
    in ``target_epsg`` (reprojected if the source differs) — or ``None`` on failure.
    """
    import os
    import tempfile

    import geopandas as gpd
    import numpy as np

    from chester import qgis_process

    qp = qgis_process.QgisProcess()
    work = tempfile.mkdtemp(prefix="chester_pc_")
    total = _pc_count(qp, pc_path)
    step = max(1, round(total / max_points)) if total else 20
    thin = os.path.join(work, "thin.laz")
    vec = os.path.join(work, "pts.gpkg")
    try:
        qp.run("pdal:thinbydecimate", {"INPUT": pc_path, "POINTS_NUMBER": step,
                                       "OUTPUT": thin, "VPC_OUTPUT_FORMAT": 0})
        qp.run("pdal:exportvector", {"INPUT": thin, "ATTRIBUTE": "Classification",
                                     "OUTPUT": vec})
    except Exception:  # noqa: BLE001
        return None
    g = gpd.read_file(vec)
    if g.empty:
        return None
    if len(g) > max_points:  # count was unknown / step too small — subsample evenly
        g = g.iloc[:: max(1, len(g) // max_points)]
    epsg = g.crs.to_epsg() if g.crs is not None else src_epsg
    xyz = np.array([[geom.x, geom.y, geom.z if geom.has_z else 0.0]
                    for geom in g.geometry], dtype="float64")
    if target_epsg and epsg and epsg != target_epsg:
        from pyproj import Transformer

        tr = Transformer.from_crs(epsg, target_epsg, always_xy=True)
        xyz[:, 0], xyz[:, 1] = tr.transform(xyz[:, 0], xyz[:, 1])
        epsg = target_epsg
    cls = (g["Classification"].to_numpy() if "Classification" in g.columns
           else np.zeros(len(g)))
    return {"xyz": xyz, "colors": _classification_colors(cls), "epsg": epsg,
            "count": int(len(g))}
