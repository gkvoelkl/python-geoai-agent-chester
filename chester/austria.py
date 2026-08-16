"""Austrian open-geodata connectors (the DACH expansion, §5.10) — Statistik Austria.

Austria's open geodata is more fragmented than Germany's or Switzerland's (no unified
national LoD2; terrain/cadastre split across BEV and the Länder). The cleanest,
most-authoritative first piece is **administrative boundaries** from **STATISTIK
AUSTRIA**'s open WFS (`www.statistik.gv.at/gs-open/GEODATA`), the AT counterpart of the
German BKG **vg250** and the Swiss **swissBOUNDARIES3D**.

- CRS is **MGI / Austria Lambert (EPSG:31287)**, metric.
- The join key is `g_id` (name `g_name`). For municipalities it is the
  **Gemeindekennziffer (GKZ)** which — like the German AGS — is **hierarchical**
  (1st digit = Bundesland, first 3 = politischer Bezirk), so a key-prefix ``match``
  selects a Bundesland's or Bezirk's members (unlike the Swiss `bfs_nummer`).

Levels: GEM (Gemeinde) · BEZIRK (politischer Bezirk) · NUTS1/2/3 (AT NUTS2 ≈ the nine
Bundesländer). One national zipped shapefile per level is fetched via WFS once and
cached, then subset per request (by ``match`` and/or ``bbox``).

Note: the statistik.gv.at TLS chain omits an intermediate certificate, so the system
CA store fails to verify it; downloads therefore use the **certifi** CA bundle (already
a dependency). Pure core (no SelmaKit dep, like ``swisstopo.py`` / ``boundaries.py``).
"""

from __future__ import annotations

import re
import ssl
from pathlib import Path
from urllib.request import Request, urlopen

_UA = {"User-Agent": "Mozilla/5.0 (Chester GeoAI)"}
_WFS = "https://www.statistik.gv.at/gs-open/GEODATA/ows"
_LICENCE = "CC-BY 4.0 — STATISTIK AUSTRIA (data.statistik.gv.at)"
_LICENCE_BEV = "CC-BY 4.0 — © BEV (data.bev.gv.at, ALS DGM)"
_LICENCE_VIENNA = "CC-BY 4.0 — Datenquelle: Stadt Wien – data.wien.gv.at (MA 41)"

# level code → (WFS typename family, admin label). Key column is g_id, name g_name.
_AT_LEVELS: dict[str, tuple[str, str]] = {
    "GEM": ("GEM", "Gemeinde (GKZ)"),
    "BEZIRK": ("POLBEZ", "Politischer Bezirk"),
    "NUTS1": ("NUTS1", "NUTS-1"),
    "NUTS2": ("NUTS2", "NUTS-2 (≈ Bundesland)"),
    "NUTS3": ("NUTS3", "NUTS-3"),
}


def _ctx() -> ssl.SSLContext:
    """SSL context using certifi — statistik.gv.at omits an intermediate cert."""
    import certifi

    return ssl.create_default_context(cafile=certifi.where())


def austria_boundary_levels() -> list[dict]:
    """Every fetchable AT admin level: level code, admin name and join key (g_id)."""
    return [{"level": lvl, "admin": label, "key": "g_id"}
            for lvl, (_fam, label) in _AT_LEVELS.items()]


_typenames: dict[str, str] | None = None


def _resolve_typenames() -> dict[str, str]:
    """Newest WFS typename date per family, from GetCapabilities (memoised)."""
    global _typenames
    if _typenames is None:
        url = f"{_WFS}?service=WFS&version=2.0.0&request=GetCapabilities"
        with urlopen(Request(url, headers=_UA), timeout=60, context=_ctx()) as r:
            doc = r.read().decode("utf-8", "replace")
        latest: dict[str, str] = {}
        for fam, date in re.findall(
                r"GEODATA:STATISTIK_AUSTRIA_([A-Z0-9]+)_(\d{8})", doc):
            if date > latest.get(fam, ""):
                latest[fam] = date
        if not latest:
            raise RuntimeError("Statistik Austria WFS: no typenames in capabilities")
        _typenames = latest
    return _typenames


def _ensure_shapezip(family: str, cache_dir: str) -> str:
    """Fetch the national layer for ``family`` once (WFS SHAPE-ZIP); cached path."""
    import shutil

    date = _resolve_typenames().get(family)
    if date is None:
        raise RuntimeError(f"Statistik Austria WFS has no layer for family {family!r}")
    typename = f"STATISTIK_AUSTRIA_{family}_{date}"
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(cache_dir) / f"{typename}.zip"
    if dest.exists() and dest.stat().st_size > 0:
        return str(dest)
    url = (f"{_WFS}?service=WFS&version=2.0.0&request=GetFeature"
           f"&typeNames=GEODATA:{typename}&outputFormat=SHAPE-ZIP")
    tmp = dest.with_suffix(".zip.part")
    with urlopen(Request(url, headers=_UA), timeout=300, context=_ctx()) as r, \
            open(tmp, "wb") as fh:
        shutil.copyfileobj(r, fh)
    tmp.replace(dest)  # atomic — a half-download never poisons the cache
    return str(dest)


def fetch_austria_boundaries(level: str, output_path: str, cache_dir: str,
                             match: str | None = None,
                             bbox_wgs84: list[float] | None = None) -> dict:
    """Fetch Austrian administrative boundary polygons for ``level`` into ``output_path``.

    ``level`` is one of GEM / BEZIRK / NUTS1 / NUTS2 / NUTS3. ``match`` filters by join-
    key (``g_id``) prefix — hierarchical for GEM/BEZIRK, so "7" = Tirol, "701" = Bezirk
    Innsbruck — **or** by name (``g_name``) substring ("Innsbruck"). ``bbox`` = [west,
    south, east, north] in WGS84 spatially windows the result. The output is a GeoPackage
    in **EPSG:31287 (MGI/Austria Lambert)** carrying ``g_id`` (the join key, GKZ for GEM)
    and ``g_name``. The Austrian counterpart of the German ``fetch_boundaries`` and the
    Swiss ``fetch_swiss_boundaries``.
    """
    import geopandas as gpd

    lvl = level.strip().upper()
    spec = _AT_LEVELS.get(lvl)
    if spec is None:
        return {"ok": False, "error": f"unknown Austrian level '{level}'",
                "known": sorted(_AT_LEVELS)}
    family, _label = spec
    try:
        zip_path = _ensure_shapezip(family, cache_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"download failed: {type(exc).__name__}: {exc}"}

    read_kwargs: dict = {}
    if bbox_wgs84:
        from pyproj import Transformer

        tr = Transformer.from_crs(4326, 31287, always_xy=True)
        _w, _s, _e, _n = bbox_wgs84
        read_kwargs["bbox"] = tuple(tr.transform_bounds(_w, _s, _e, _n))
    try:
        gdf = gpd.read_file(zip_path, **read_kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"read failed: {type(exc).__name__}: {exc}"}

    if match:
        m = match.strip()
        key = gdf["g_id"].astype(str)
        name = gdf["g_name"].astype(str).str.lower()
        gdf = gdf[key.str.startswith(m) | name.str.contains(m.lower(), regex=False)]

    if gdf.empty:
        return {"ok": False, "error": f"no {lvl} units matched"
                + (f" '{match}'" if match else "")
                + (" in the bbox" if bbox_wgs84 else "")}

    gdf = gdf[["g_id", "g_name", "geometry"]]
    gdf.to_file(output_path, driver="GPKG", layer=family.lower())
    return {
        "ok": True,
        "output": str(output_path),
        "dataset": "statistik-austria",
        "level": lvl,
        "key_column": "g_id",
        "units": int(len(gdf)),
        "crs": "EPSG:31287",
        "licence": _LICENCE,
        "note": "STATISTIK AUSTRIA boundaries in EPSG:31287 (MGI/Austria Lambert). "
        "g_id is the join key (GKZ for GEM, hierarchical); g_name the name.",
    }


# ── DGM / terrain (BEV ALS 1 m) ───────────────────────────────────────────────────
#
# The national open 1 m terrain model: BEV's Airborne-Laserscan DTM, published as 55
# **Cloud-Optimized GeoTIFF** tiles (50 km grid, EPSG:3035) at a fixed public path. Each
# tile is huge (50 km @ 1 m) but COG-tiled with overviews, so — like ``fetch_dem`` /
# ``fetch_swissalti3d`` — we window-read just the bbox over ``/vsicurl`` and mosaic,
# never downloading a whole 180 MB tile. The AT counterpart of ``fetch_dgm1`` /
# ``fetch_swissalti3d``. (TLS again needs the certifi CA bundle → GDAL_HTTP_CAINFO.)

_ALS_TILE_INDEX = "https://data.bev.gv.at/download/ALS/ALS_Kacheluebersicht.zip"
_ALS_DTM_BASE = "https://data.bev.gv.at/download/ALS/DTM/20190915"  # current open release
_ALS_NODATA = -9999.0


def _ensure_als_index(cache_dir: str):
    """Download+cache the BEV ALS tile index (55 tiles, EPSG:3035); return a GeoDataFrame."""
    import shutil

    import geopandas as gpd

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(cache_dir) / "als_kacheluebersicht.zip"
    if not (dest.exists() and dest.stat().st_size > 0):
        tmp = dest.with_suffix(".zip.part")
        with urlopen(Request(_ALS_TILE_INDEX, headers=_UA), timeout=120,
                     context=_ctx()) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh)
        tmp.replace(dest)
    return gpd.read_file(dest)


def fetch_austria_dem(bbox_wgs84: list[float], output_path: str, cache_dir: str,
                      max_tiles: int = 4) -> dict:
    """Fetch the Austrian 1 m ALS terrain model (BEV DGM) for ``bbox`` into a GeoTIFF.

    ``bbox`` = [west, south, east, north] in WGS84 (Austria only). Finds the covering
    BEV ALS tiles via the tile index, window-reads each covering **COG** over the bbox
    (`/vsicurl`, no full-tile download) and mosaics them into one single-band GeoTIFF in
    **EPSG:3035 (LAEA, metres)** — Austrian fine terrain (slope/area work directly; the
    DTM half of a DSM−DTM). The Austrian counterpart of ``fetch_dgm1`` (DE) /
    ``fetch_swissalti3d`` (CH); nodata is preserved as -9999.
    """
    import certifi
    import rasterio
    from pyproj import Transformer
    from rasterio.merge import merge
    from shapely.geometry import box

    try:
        idx = _ensure_als_index(cache_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"tile index download failed: "
                f"{type(exc).__name__}: {exc}"}

    tr = Transformer.from_crs(4326, 3035, always_xy=True)
    _w, _s, _e, _n = bbox_wgs84
    minx, miny, maxx, maxy = tr.transform_bounds(_w, _s, _e, _n)
    sel = idx[idx.intersects(box(minx, miny, maxx, maxy))]
    if sel.empty:
        return {"ok": False, "error": "no BEV ALS tiles cover the bbox (Austria only)",
                "bbox": bbox_wgs84}
    tiles = list(sel["GRD_ID"])
    if len(tiles) > max_tiles:
        return {"ok": False, "error": f"bbox needs {len(tiles)} tiles (> {max_tiles}) — "
                "narrow it.", "tiles": len(tiles)}

    urls = [f"/vsicurl/{_ALS_DTM_BASE}/{t}.tif" for t in tiles]
    env = rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                       CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                       GDAL_HTTP_MULTIRANGE="YES", GDAL_HTTP_CAINFO=certifi.where())
    try:
        with env:
            datasets = [rasterio.open(u) for u in urls]
            try:
                mosaic, transform = merge(datasets, bounds=(minx, miny, maxx, maxy),
                                          nodata=_ALS_NODATA)
                profile = datasets[0].profile
            finally:
                for d in datasets:
                    d.close()
            profile.update(driver="GTiff", count=1, height=mosaic.shape[1],
                           width=mosaic.shape[2], transform=transform,
                           nodata=_ALS_NODATA, compress="lzw")
            with rasterio.open(output_path, "w", **profile) as dst:
                dst.write(mosaic[0], 1)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "output": str(output_path),
        "size": [int(mosaic.shape[2]), int(mosaic.shape[1])],
        "resolution_m": 1.0,
        "crs": "EPSG:3035",
        "tiles_used": len(tiles),
        "licence": _LICENCE_BEV,
        "note": "BEV ALS 1 m DGM in EPSG:3035 (LAEA, metres) — slope/area work directly. "
        "nodata = -9999 (mask it for slope/stats).",
    }


# ── Vienna 3D buildings (LOD2.1 roof model → CityJSON) ─────────────────────────────
#
# Vienna publishes an open **LOD2.1** generalised roof model as **CityGML** (CC-BY,
# MA 41 Stadtvermessung) — full semantic surfaces (Ground/Wall/Roof + measuredHeight),
# EPSG:31256 (MGI/Austria GK M34). It feeds Chester's existing CityGML→CityJSON pipeline
# (``citymodel.write_cityjson``) directly, so the output drives the same 3D renderers as
# the German LoD2. The whole city is distributed as per-tile files through Vienna's OGD
# download portal (interactive, no clean per-bbox URL); Chester therefore takes a
# **local CityGML/zip path** (fetched from that portal) — or ``"sample"`` for the public
# demo tile — the honest, portal-independent route (cf. the gated-GTFS local-path hatch).

_VIENNA_LOD2_SAMPLE = "https://www.wien.gv.at/downloads/ma41/dach-lod2-gml.zip"


def _gml_paths_from(source: str, cache_dir: str) -> list[str]:
    """Resolve ``source`` (``"sample"`` / a local .gml / a local-or-sample .zip) → gml paths."""
    import shutil
    import zipfile

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    if source == "sample":
        dest = Path(cache_dir) / "vienna_lod2_sample.zip"
        if not (dest.exists() and dest.stat().st_size > 0):
            tmp = dest.with_suffix(".zip.part")
            with urlopen(Request(_VIENNA_LOD2_SAMPLE, headers=_UA), timeout=120) as r, \
                    open(tmp, "wb") as fh:
                shutil.copyfileobj(r, fh)
            tmp.replace(dest)
        zip_path = str(dest)
    else:
        p = Path(source)
        if not p.exists():
            raise FileNotFoundError(f"Vienna source not found: {source}")
        if p.suffix.lower() == ".gml":
            return [str(p)]
        zip_path = str(p)

    out_dir = Path(cache_dir) / (Path(zip_path).stem + "_gml")
    out_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out_dir)
    return [str(p) for p in out_dir.rglob("*.gml")]


def fetch_vienna_buildings(output_path: str, cache_dir: str,
                           source: str = "sample",
                           bbox_wgs84: list[float] | None = None) -> dict:
    """Convert Vienna's open LOD2.1 CityGML roof model to a **CityJSON** model.

    ``source`` is ``"sample"`` (Vienna's public demo tile, downloaded) **or a local path**
    to a CityGML ``.gml`` / a ``.zip`` of tiles downloaded from Vienna's OGD portal
    (`www.wien.gv.at/stadtplanung/generalisiertes-dachmodell`) — Vienna ships the whole
    city per-tile through that interactive portal, so pass the tile(s) you fetched.
    Writes a CityJSON 1.1 model in **EPSG:31256** (optionally clipped to ``bbox``); feed
    the result to ``render_buildings_3d`` / ``qgis_show_3d`` / ``cityjson_to_geopackage``.
    """
    from chester import citymodel

    try:
        gml_paths = _gml_paths_from(source, cache_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not gml_paths:
        return {"ok": False, "error": "no .gml found in the Vienna source"}

    try:
        if bbox_wgs84:
            full = str(Path(output_path).with_suffix(".full.json"))
            citymodel.write_cityjson(gml_paths, full, epsg=31256)
            r = citymodel.subset_bbox(full, output_path, bbox_wgs84, epsg=31256)
        else:
            r = citymodel.write_cityjson(gml_paths, output_path, epsg=31256)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"CityGML→CityJSON failed: "
                f"{type(exc).__name__}: {exc}"}
    if not r.get("ok", True):
        return r

    return {
        "ok": True,
        "output": str(output_path),
        "dataset": "vienna-lod2",
        "buildings": r.get("buildings"),
        "crs": "EPSG:31256",
        "tiles": len(gml_paths),
        "licence": _LICENCE_VIENNA,
        "note": "Vienna LOD2.1 roof model → CityJSON (EPSG:31256). Full city = per-tile "
        "via Vienna's OGD portal; pass a downloaded .gml/.zip as source, or 'sample' for "
        "the demo tile.",
    }
