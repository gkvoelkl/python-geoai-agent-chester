"""swisstopo connectors — Switzerland's open geodata (the DACH extension, §5.10).

Chester's authoritative data is Germany-specific; Switzerland is the first
neighbour added. swisstopo publishes its open geodata (free, attribution) through a
**STAC API** (`data.geo.admin.ch`), so Chester reuses the STAC access pattern. CRS
is **LV95 / EPSG:2056** throughout.

Connectors:

- **swissALTI3D** (``fetch_swissalti3d``) — the high-resolution digital terrain model
  (0.5 m / 2 m Cloud-Optimized GeoTIFF tiles). The Swiss analogue of ``fetch_dgm1``:
  mosaics the covering tiles for a bbox into one GeoTIFF (metric, EPSG:2056) — fine
  terrain, the DTM half of a DSM−DTM, or the relief under the 3D buildings. Tool on
  ``DataDiscoveryCapability`` next to ``fetch_dem`` / ``fetch_dgm1``.
- **swissBUILDINGS3D 3.0** (``fetch_swissbuildings3d``) — the Swiss 3D building model,
  the counterpart of the German LoD2 ``fetch_cityjson``. It ships as a FileGDB of ESRI
  **MultiPatch** solids (no CityGML), so we read it with GDAL (``ogr2ogr``, bundled
  with QGIS) and turn each solid into a CityJSON LoD2 MultiSurface via
  ``citymodel.cityjson_from_solids`` — same downstream renderers as the German path.
  Tool on ``GeoCityModelCapability``.
- **swissBOUNDARIES3D** (``fetch_swissboundaries3d``) — the Swiss administrative units
  (Gemeinde / Bezirk / Kanton / Land), the Swiss counterpart of the German BKG
  ``fetch_boundaries``. One national GeoPackage per yearly STAC item; downloaded and
  cached once, then subset by level / ``match`` / ``bbox``. The Gemeinde layer carries
  ``bfs_nummer`` (the Swiss BFS/OFS statistics key, the AGS analogue) and
  ``einwohnerzahl`` (population), so a population choropleth needs no separate table.
  Tool on ``GeoBoundariesCapability`` next to ``fetch_boundaries``.

Pure core (no SelmaKit dep, like ``lod2.py``).
"""

from __future__ import annotations

import math
from pathlib import Path
from urllib.request import Request, urlopen

_UA = {"User-Agent": "Mozilla/5.0 (Chester Geo-AI)"}
_STAC = "https://data.geo.admin.ch/api/stac/v1"
_ALTI3D = "ch.swisstopo.swissalti3d"
_BUILD3D = "ch.swisstopo.swissbuildings3d_3_0"  # swissBUILDINGS3D 3.0 (FileGDB, MultiPatch)
_BOUND3D = "ch.swisstopo.swissboundaries3d"  # swissBOUNDARIES3D (national GeoPackage/year)
_TLMREGIO = "ch.swisstopo.swisstlmregio"  # swissTLMRegio (generalised topographic vector)
# swisstopo publishes swissALTI3D as free open geodata (source citation required);
# the STAC `license` field reads "proprietary" but the actual terms are free (OGD).
_SWISSTOPO_LICENCE = "© swisstopo — free open geodata (BGDI, attribution required)"


def _stac_items(collection: str, bbox_wgs84, max_items: int = 200) -> list:
    """All STAC items of ``collection`` intersecting ``bbox`` (follows pagination)."""
    import json

    w, s, e, n = bbox_wgs84
    url: str | None = f"{_STAC}/collections/{collection}/items?bbox={w},{s},{e},{n}&limit=100"
    feats: list = []
    while url and len(feats) < max_items:
        with urlopen(Request(url, headers=_UA), timeout=40) as r:
            doc = json.loads(r.read())
        feats.extend(doc.get("features", []))
        url = next((lk["href"] for lk in doc.get("links", [])
                    if lk.get("rel") == "next"), None)
    return feats


def _alti3d_hrefs(features: list, resolution: float) -> list:
    """The COG GeoTIFF asset href per tile at the given ``resolution`` (0.5 or 2)."""
    hrefs = []
    for f in features:
        for a in f.get("assets", {}).values():
            href = a.get("href", "")
            if href.endswith(".tif") and float(a.get("gsd", 0)) == float(resolution):
                hrefs.append(href)
                break
    return hrefs


def fetch_swissalti3d(bbox_wgs84: list[float], output_path: str,
                      resolution: float = 2.0, max_tiles: int = 64) -> dict:
    """Fetch the swissALTI3D DTM for ``bbox`` into ``output_path`` (a GeoTIFF).

    ``bbox`` = [west, south, east, north] in WGS84 (Switzerland only). Finds the
    covering swissALTI3D tiles via the swisstopo STAC API, mosaics the
    ``resolution`` (2 m default, or 0.5 m) Cloud-Optimized GeoTIFFs clipped to the
    bbox, and writes a single-band GeoTIFF in **EPSG:2056 (LV95, metres)** — Swiss
    fine terrain (slope/area work directly; the DTM half of a DSM−DTM; the relief
    under 3D buildings). Returns size, CRS and licence.
    """
    import rasterio
    from pyproj import Transformer
    from rasterio.merge import merge

    try:
        feats = _stac_items(_ALTI3D, bbox_wgs84)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"swisstopo STAC query failed: "
                f"{type(exc).__name__}: {exc}"}
    hrefs = _alti3d_hrefs(feats, resolution)
    if not hrefs:
        return {"ok": False, "error": "no swissALTI3D tiles cover the bbox "
                "(Switzerland only)", "bbox": bbox_wgs84}
    if len(hrefs) > max_tiles:
        return {"ok": False, "error": f"bbox needs {len(hrefs)} tiles (> {max_tiles}) "
                f"at {resolution} m — narrow it or use resolution=2.", "tiles": len(hrefs)}

    tr = Transformer.from_crs(4326, 2056, always_xy=True)
    _w, _s, _e, _n = bbox_wgs84
    minx, miny, maxx, maxy = tr.transform_bounds(_w, _s, _e, _n)
    env = rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                       GDAL_HTTP_MULTIRANGE="YES",
                       CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif")
    try:
        with env:
            datasets = [rasterio.open(h) for h in hrefs]
            try:
                mosaic, transform = merge(datasets, bounds=(minx, miny, maxx, maxy))
                profile = datasets[0].profile
                crs = str(datasets[0].crs)
            finally:
                for d in datasets:
                    d.close()
            profile.update(driver="GTiff", count=1, height=mosaic.shape[1],
                           width=mosaic.shape[2], transform=transform, compress="lzw")
            with rasterio.open(output_path, "w", **profile) as dst:
                dst.write(mosaic[0], 1)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "output": str(output_path),
        "size": [int(mosaic.shape[2]), int(mosaic.shape[1])],
        "resolution_m": float(resolution),
        "crs": crs or "EPSG:2056",
        "tiles_used": len(hrefs),
        "licence": _SWISSTOPO_LICENCE,
        "note": f"swissALTI3D {resolution} m in EPSG:2056 (LV95, metres) — slope/area "
        "work directly; no reprojection needed.",
    }


# ── swissBUILDINGS3D 3.0 → CityJSON (3D building models) ──────────────────────────
#
# swissBUILDINGS3D ships as a FileGDB of ESRI **MultiPatch** solids (no CityGML), so
# the CityJSON pipeline can't parse it directly. We read the GDB with GDAL: the
# ``Building_solid`` layer holds one closed 3D solid per building. pyogrio can't read
# MultiPatch, so we shell to GDAL's ``ogr2ogr`` (bundled with QGIS — already a hard
# Chester dependency) to convert MultiPatch → MultiPolygon Z (bbox-clipped) into a
# GeoPackage geopandas reads. Each solid → a CityJSON LoD2 MultiSurface via
# ``citymodel.cityjson_from_solids``, so the output feeds the same renderers /
# GeoPackage / ``qgis_show_3d`` tools as the German LoD2 path. No Java anywhere.


def _ogr2ogr_env():
    """The bundled ``ogr2ogr`` path + a GDAL-ready env (GDAL_DATA/PROJ_DATA set)."""
    from chester import qgis_env

    e = qgis_env.resolve_qgis_env()
    return str(Path(e.bin).parent / "ogr2ogr"), e.subprocess_env()


def _download_gdb_tiles(bbox_wgs84, cache_dir: str, max_tiles: int) -> dict:  # noqa: C901
# C901-Ausnahme: gekachelte gegen nationale Auslieferung, Jahrgangswahl, ogr2ogr-Umweg fuer
# MultiPatch
    """Download the covering swissBUILDINGS3D ``.gdb.zip`` tiles into ``cache_dir``."""
    import shutil

    try:
        feats = _stac_items(_BUILD3D, bbox_wgs84)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"swisstopo STAC query failed: "
                f"{type(exc).__name__}: {exc}"}
    # swissBUILDINGS3D 3.0 offers two release schemes for a footprint: per-map-sheet
    # **tiles** (filename carries an `NNNN-NN` id, ~30 MB, several years) and yearly
    # **national bulk** GDBs (no tile id, multi-GB — unusable for a bbox fetch). Keep
    # only the tiled releases, and per tile only the most recent year.
    import re

    tile_re = re.compile(r"_(\d{4}-\d{2})_2056_")
    by_tile: dict[str, tuple[int, str]] = {}
    for f in feats:
        for a in f.get("assets", {}).values():
            href = a.get("href", "")
            if not href.endswith(".gdb.zip"):
                continue
            name = href.rsplit("/", 1)[-1]
            m = tile_re.search(name)
            if not m:  # national bulk release (no tile id) — far too large, skip
                continue
            year = int(name.split("_3_0_")[1][:4])
            tid = m.group(1)
            if tid not in by_tile or year > by_tile[tid][0]:
                by_tile[tid] = (year, href)
            break
    hrefs = [h for _, h in by_tile.values()]
    if not hrefs:
        return {"ok": False, "error": "no tiled swissBUILDINGS3D release covers the "
                "bbox (Switzerland only)", "bbox": bbox_wgs84}
    if len(hrefs) > max_tiles:
        return {"ok": False, "error": f"bbox needs {len(hrefs)} tiles (> {max_tiles}) "
                f"— narrow it (a swissBUILDINGS3D tile is 5 km).", "tiles": len(hrefs)}

    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    paths = []
    for href in hrefs:
        dest = Path(cache_dir) / href.rsplit("/", 1)[-1]
        if not dest.exists():
            try:
                with urlopen(Request(href, headers=_UA), timeout=120) as r, \
                        open(dest, "wb") as fh:
                    shutil.copyfileobj(r, fh)
            except Exception as exc:  # noqa: BLE001
                dest.unlink(missing_ok=True)
                return {"ok": False, "error": f"tile download failed: "
                        f"{type(exc).__name__}: {exc}", "href": href}
        paths.append(str(dest))
    return {"ok": True, "paths": paths}


def _solids_from_gdb(gdb_zip: str, bbox_wgs84, ogr2ogr: str, env: dict, work_dir: str):
    """MultiPatch ``Building_solid`` → building solids clipped to ``bbox`` (via ogr2ogr).

    Yields ``(obj_id, attributes, faces)`` per building — ``faces`` a list of exterior
    rings ``(x, y, z)`` in EPSG:2056, ``attributes`` carrying the geometric
    ``measured_height`` plus the source roof/type fields.
    """
    import subprocess

    import geopandas as gpd

    w, s, e, n = bbox_wgs84
    out = Path(work_dir) / (Path(gdb_zip).stem + "_bs.gpkg")
    out.unlink(missing_ok=True)
    cmd = [ogr2ogr, "-f", "GPKG", str(out), f"/vsizip/{gdb_zip}", "Building_solid",
           "-spat", str(w), str(s), str(e), str(n), "-spat_srs", "EPSG:4326",
           "-nlt", "MULTIPOLYGON25D"]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"ogr2ogr failed: {r.stderr.strip()[:300]}")
    if not out.exists():
        return
    gdf = gpd.read_file(out)
    for i, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        faces: list = []
        zs: list[float] = []
        for poly in polys:
            ring = [(c[0], c[1], c[2] if len(c) > 2 else 0.0)
                    for c in poly.exterior.coords[:-1]]  # drop the closing duplicate
            if len(ring) >= 3:
                faces.append(ring)
                zs.extend(c[2] for c in ring)
        if not faces:
            continue
        attrs = {"measured_height": round(max(zs) - min(zs), 2)}
        for src, dst in (("OBJEKTART", "objektart"), ("DACH_MAX", "roof_max_z"),
                         ("DACH_MIN", "roof_min_z"), ("GEBAEUDE_NUTZUNG", "usage"),
                         ("EGID", "egid")):
            v = row.get(src)
            if v is None or v == "" or (isinstance(v, float) and math.isnan(v)):
                continue
            attrs[dst] = int(v) if dst == "egid" else v
        uuid = str(row.get("UUID") or f"bldg_{i}").strip("{}")
        yield (uuid, attrs, faces)


def fetch_swissbuildings3d(bbox_wgs84: list[float], output_path: str,
                           cache_dir: str, max_tiles: int = 4) -> dict:
    """Fetch swissBUILDINGS3D 3.0 for ``bbox`` and write a **CityJSON** model.

    ``bbox`` = [west, south, east, north] in WGS84 (Switzerland only). Downloads the
    covering swissBUILDINGS3D 3.0 FileGDB tiles (cached in ``cache_dir``), reads the
    ``Building_solid`` MultiPatch layer via GDAL (clipped to the bbox), and writes a
    CityJSON 1.1 model in **EPSG:2056 (LV95)** — one LoD2 MultiSurface per building
    with a geometry-derived ``measured_height``. The Swiss analogue of
    ``fetch_cityjson``; feed the result to ``render_buildings_3d`` / ``qgis_show_3d`` /
    ``cityjson_to_geopackage``.
    """
    from chester import citymodel

    dl = _download_gdb_tiles(bbox_wgs84, cache_dir, max_tiles)
    if not dl.get("ok"):
        return dl
    try:
        ogr2ogr, env = _ogr2ogr_env()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"GDAL/ogr2ogr not available (needs QGIS): "
                f"{type(exc).__name__}: {exc}"}

    buildings = []
    try:
        for tile in dl["paths"]:
            buildings.extend(_solids_from_gdb(tile, bbox_wgs84, ogr2ogr, env, cache_dir))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not buildings:
        return {"ok": False, "error": "no buildings in the bbox", "bbox": bbox_wgs84}

    cj = citymodel.cityjson_from_solids(buildings, epsg=2056)
    import json

    Path(output_path).write_text(json.dumps(cj), encoding="utf-8")
    return {
        "ok": True,
        "output": str(output_path),
        "buildings": len(cj["CityObjects"]),
        "vertices": len(cj["vertices"]),
        "crs": "EPSG:2056",
        "tiles_used": len(dl["paths"]),
        "licence": _SWISSTOPO_LICENCE,
        "note": "swissBUILDINGS3D 3.0 solids → CityJSON LoD2 (EPSG:2056); "
        "measured_height is the geometric height of each 3D solid.",
    }


# ── swissBOUNDARIES3D → administrative-boundary polygons ──────────────────────────
#
# The Swiss counterpart of the German BKG ``boundaries.fetch_boundaries``: official
# administrative units as polygons, carrying the join keys Swiss statistics use.
# swissBOUNDARIES3D ships one **national** GeoPackage per yearly STAC item (no tiling,
# ~37 MB zipped), so — like the BKG connector — we download+unzip the newest release
# once into the shared ``_boundaries`` cache dir, then subset per request (level,
# key/name ``match``, bbox). CRS is EPSG:2056 (LV95) and geometries are 3D
# (MultiPolygon Z); Z is harmless for a 2D choropleth/clip and kept as-is.

# Swiss admin level → (GeoPackage layer, join-key column, name column, admin label,
# attribute columns to keep in the subset). Order = coarse→fine for the catalog.
_SWISS_BOUNDARY_LEVELS: dict[str, tuple[str, str, str, str, tuple[str, ...]]] = {
    "LAND": ("tlm_landesgebiet", "icc", "name", "Land / Staat",
             ("name", "icc")),
    "KANTON": ("tlm_kantonsgebiet", "kantonsnummer", "name", "Kanton",
               ("kantonsnummer", "name", "einwohnerzahl", "icc")),
    "BEZIRK": ("tlm_bezirksgebiet", "bezirksnummer", "name", "Bezirk",
               ("bezirksnummer", "kantonsnummer", "name", "einwohnerzahl", "icc")),
    "GEMEINDE": ("tlm_hoheitsgebiet", "bfs_nummer", "name", "Gemeinde (BFS-Nummer)",
                 ("bfs_nummer", "name", "bezirksnummer", "kantonsnummer",
                  "einwohnerzahl", "gem_flaeche", "icc")),
}


def swiss_boundary_levels() -> list[dict]:
    """Every fetchable Swiss admin level: level code, admin name and join key."""
    return [{"level": lvl, "admin": label, "key": key}
            for lvl, (_layer, key, _name, label, _keep) in _SWISS_BOUNDARY_LEVELS.items()]


def _latest_gpkg_href(features: list) -> str:
    """The newest release's national GeoPackage-zip href (max item id → its .gpkg.zip)."""
    if not features:
        raise RuntimeError("swissBOUNDARIES3D: no STAC items")
    latest = max(features, key=lambda f: f.get("id", ""))  # ids sort lexically by date
    for a in latest.get("assets", {}).values():
        href = a.get("href", "")
        if href.endswith(".gpkg.zip"):
            return href
    raise RuntimeError(f"swissBOUNDARIES3D: no .gpkg.zip asset in {latest.get('id')!r}")


def _collection_items(collection: str) -> list:
    """All STAC items of a national ``collection`` (releases, ~one per year)."""
    import json

    url: str | None = f"{_STAC}/collections/{collection}/items?limit=100"
    feats: list = []
    while url:
        with urlopen(Request(url, headers=_UA), timeout=40) as r:
            doc = json.loads(r.read())
        feats.extend(doc.get("features", []))
        url = next((lk["href"] for lk in doc.get("links", [])
                    if lk.get("rel") == "next"), None)
    return feats


def _ensure_swissboundaries_gpkg(cache_dir: str) -> str:
    """Download+unzip the newest national swissBOUNDARIES3D GeoPackage once; cached path."""
    import io
    import shutil
    import zipfile

    href = _latest_gpkg_href(_collection_items(_BOUND3D))
    stamp = href.rsplit("/", 1)[-1][: -len(".gpkg.zip")]  # release id → a new year re-downloads
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(cache_dir) / f"{stamp}.gpkg"
    if dest.exists() and dest.stat().st_size > 0:
        return str(dest)
    with urlopen(Request(href, headers=_UA), timeout=300) as r:
        blob = r.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        member = next((n for n in zf.namelist() if n.lower().endswith(".gpkg")), None)
        if member is None:
            raise RuntimeError(f"no .gpkg inside {href}")
        with zf.open(member) as fh, open(dest, "wb") as out:
            shutil.copyfileobj(fh, out)
    return str(dest)


def _resolve_kantonsnummer(gpkg: str, canton, ch_only: bool = True) -> int:
    """Canton name or number → its ``kantonsnummer`` (via the KANTON layer)."""
    import geopandas as gpd

    s = str(canton).strip()
    if s.isdigit():
        return int(s)
    k = gpd.read_file(gpkg, layer="tlm_kantonsgebiet")
    if ch_only and "icc" in k.columns:
        k = k[k["icc"] == "CH"]
    hit = k[k["name"].astype(str).str.lower().str.contains(s.lower(), regex=False)]
    if hit.empty:
        raise ValueError(f"no canton matching '{canton}' "
                         f"(known: {sorted(k['name'].astype(str))})")
    return int(hit.iloc[0]["kantonsnummer"])


def fetch_swissboundaries3d(level: str, output_path: str, cache_dir: str,  # noqa: C901
# C901-Ausnahme: Ebenen-, Kanton-, bbox- und ch_only-Filter, jeder optional
                            match: str | None = None,
                            bbox_wgs84: list[float] | None = None,
                            canton: str | int | None = None,
                            ch_only: bool = True) -> dict:
    """Fetch Swiss administrative boundary polygons for ``level`` into ``output_path``.

    ``level`` is one of LAND / KANTON / BEZIRK / GEMEINDE. To select **all units of a
    canton** (e.g. all Gemeinden of Kanton Bern) pass ``canton`` (name "Bern" or number
    2) — the Swiss ``bfs_nummer`` is **not** hierarchical, so ``match`` cannot do this.
    ``match`` filters by join-key prefix **or** name substring ("Bern") — use it to find
    a *named* unit, not a canton's members. ``bbox`` = [west, south, east, north] in
    WGS84 spatially windows the result. ``ch_only`` (default) keeps ``icc == "CH"``
    units, dropping the Liechtenstein / foreign-enclave polygons the dataset also
    carries. The output is a GeoPackage in **EPSG:2056 (LV95)** carrying the join key —
    GEMEINDE also carries ``bfs_nummer`` (the Swiss statistics key) and ``einwohnerzahl``
    (population), so a population choropleth needs no separate table. The Swiss
    counterpart of the German BKG ``fetch_boundaries``.
    """
    import geopandas as gpd

    lvl = level.strip().upper()
    spec = _SWISS_BOUNDARY_LEVELS.get(lvl)
    if spec is None:
        return {"ok": False, "error": f"unknown Swiss level '{level}'",
                "known": sorted(_SWISS_BOUNDARY_LEVELS)}
    layer, key_col, name_col, _label, keep_cols = spec
    try:
        gpkg = _ensure_swissboundaries_gpkg(cache_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"download failed: {type(exc).__name__}: {exc}"}

    read_kwargs: dict = {"layer": layer}
    if bbox_wgs84:
        from pyproj import Transformer

        tr = Transformer.from_crs(4326, 2056, always_xy=True)
        _w, _s, _e, _n = bbox_wgs84
        read_kwargs["bbox"] = tuple(tr.transform_bounds(_w, _s, _e, _n))
    try:
        gdf = gpd.read_file(gpkg, **read_kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"read failed: {type(exc).__name__}: {exc}"}

    if ch_only and "icc" in gdf.columns:
        gdf = gdf[gdf["icc"] == "CH"]

    if canton is not None:
        if "kantonsnummer" not in gdf.columns:
            return {"ok": False, "error": f"level {lvl} has no kantonsnummer — "
                    "canton filtering applies to KANTON/BEZIRK/GEMEINDE only"}
        try:
            knum = _resolve_kantonsnummer(gpkg, canton, ch_only)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"canton resolve failed: {exc}"}
        gdf = gdf[gdf["kantonsnummer"].fillna(-1).astype(int) == knum]

    if match:
        m = match.strip()
        key = gdf[key_col].astype(str)
        name = gdf[name_col].astype(str).str.lower()
        gdf = gdf[key.str.startswith(m) | name.str.contains(m.lower(), regex=False)]

    if gdf.empty:
        return {"ok": False, "error": f"no {lvl} units matched"
                + (f" '{match}'" if match else "")
                + (f" in canton {canton}" if canton is not None else "")
                + (" in the bbox" if bbox_wgs84 else "")}

    keep = [c for c in keep_cols if c in gdf.columns] + ["geometry"]
    gdf = gdf[keep]
    gdf.to_file(output_path, driver="GPKG", layer=layer)

    return {
        "ok": True,
        "output": str(output_path),
        "dataset": "swissboundaries3d",
        "level": lvl,
        "key_column": key_col,
        "units": int(len(gdf)),
        "crs": "EPSG:2056",
        "licence": _SWISSTOPO_LICENCE,
        "note": "swissBOUNDARIES3D in EPSG:2056 (LV95). GEMEINDE carries bfs_nummer "
        "(the Swiss BFS/OFS statistics key) and einwohnerzahl (population) — a "
        "population choropleth needs no separate table.",
    }


# ── swissTLMRegio → topographic vector (roads / hydrography / land cover / …) ──────
#
# The Swiss topographic vector layer. The **full** swissTLM3D (1:10 000) ships only as
# a single ~4.5 GB deflate-compressed national bundle — no tiled release, no per-bbox
# vector API, and the zip's compression defeats GDAL's ``/vsizip//vsicurl/`` random
# access — so a per-bbox fetch of full TLM3D is infeasible. **swissTLMRegio** is the
# generalised (≈1:200 000) national model: one ~155 MB GeoPackage per yearly STAC item,
# small enough to download+cache once (like swissBOUNDARIES3D) and then subset by theme
# + bbox. It covers roads, railways, buildings, land cover, hydrography and POIs in
# EPSG:2056 — the authoritative-Swiss counterpart to a global OSM ``osm_features`` pull.
# (For the full-resolution TLM3D, point users at ``osm_features`` for a per-bbox route.)

# The zip bundles two GeoPackages; the topographic themes live in the Product one.
_TLMREGIO_PRODUCT_GPKG = "swissTLMRegio_Product_LV95.gpkg"

# friendly theme → Product-GeoPackage layer, with a one-line description for the catalog.
_TLMREGIO_THEMES: dict[str, tuple[str, str]] = {
    "roads": ("tlmregio_transportation_road", "road network (lines)"),
    "railways": ("tlmregio_transportation_railway", "railway network (lines)"),
    "buildings": ("tlmregio_buildings_building", "building footprints (polygons)"),
    "landcover": ("tlmregio_landcover_landcover", "land cover (polygons)"),
    "lakes": ("tlmregio_hydrography_lake", "lakes / standing water (polygons)"),
    "rivers": ("tlmregio_hydrography_flowingwater", "rivers / flowing water (lines)"),
    "builtup": ("tlmregio_miscellaneous_buildupp", "built-up areas (points)"),
    "poi": ("tlmregio_miscellaneous_poi", "points of interest (points)"),
    "names": ("tlmregio_names_namedlocation", "named locations / place names (points)"),
}


def tlmregio_themes() -> list[dict]:
    """The fetchable swissTLMRegio themes: theme name, GeoPackage layer, description."""
    return [{"theme": t, "layer": layer, "description": desc}
            for t, (layer, desc) in _TLMREGIO_THEMES.items()]


def _ensure_tlmregio_gpkg(cache_dir: str) -> str:
    """Download+unzip the newest national swissTLMRegio Product GeoPackage once."""
    import io
    import shutil
    import zipfile

    href = _latest_gpkg_href(_collection_items(_TLMREGIO))
    stamp = href.rsplit("/", 1)[-1][: -len(".gpkg.zip")]  # release id → a new year re-downloads
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(cache_dir) / f"{stamp}_product.gpkg"
    if dest.exists() and dest.stat().st_size > 0:
        return str(dest)
    with urlopen(Request(href, headers=_UA), timeout=600) as r:
        blob = r.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        member = next((n for n in zf.namelist()
                       if n.rsplit("/", 1)[-1] == _TLMREGIO_PRODUCT_GPKG), None)
        if member is None:  # fall back to any .gpkg if the naming changes
            member = next((n for n in zf.namelist() if n.lower().endswith(".gpkg")), None)
        if member is None:
            raise RuntimeError(f"no Product .gpkg inside {href}")
        with zf.open(member) as fh, open(dest, "wb") as out:
            shutil.copyfileobj(fh, out)
    return str(dest)


def fetch_swisstlmregio(theme: str, output_path: str, cache_dir: str,
                        bbox_wgs84: list[float] | None = None,
                        max_features: int = 200_000) -> dict:
    """Fetch a swissTLMRegio topographic ``theme`` into ``output_path`` (a GeoPackage).

    ``theme`` is one of roads / railways / buildings / landcover / lakes / rivers /
    builtup / poi / names (``tlmregio_themes()`` lists them). ``bbox`` = [west, south,
    east, north] in WGS84 windows the result; without a bbox the whole national layer
    is returned unless it exceeds ``max_features`` (then a bbox is required). The output
    is a GeoPackage in **EPSG:2056 (LV95)**. swissTLMRegio is the generalised
    (≈1:200 000) authoritative Swiss topographic model — the full-resolution swissTLM3D
    has no per-bbox route, so for finer detail use ``osm_features``.
    """
    import geopandas as gpd
    import pyogrio

    key = theme.strip().lower()
    spec = _TLMREGIO_THEMES.get(key)
    if spec is None:
        return {"ok": False, "error": f"unknown theme '{theme}'",
                "known": sorted(_TLMREGIO_THEMES)}
    layer, _desc = spec
    try:
        gpkg = _ensure_tlmregio_gpkg(cache_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"download failed: {type(exc).__name__}: {exc}"}

    read_kwargs: dict = {"layer": layer}
    if bbox_wgs84:
        from pyproj import Transformer

        tr = Transformer.from_crs(4326, 2056, always_xy=True)
        _w, _s, _e, _n = bbox_wgs84
        read_kwargs["bbox"] = tuple(tr.transform_bounds(_w, _s, _e, _n))
    else:
        try:
            n = pyogrio.read_info(gpkg, layer=layer)["features"]
        except Exception:  # noqa: BLE001
            n = 0
        if n > max_features:
            return {"ok": False, "error": f"'{key}' has {n} features nationwide "
                    f"(> {max_features}) — pass a bbox to window it.", "features": n}

    try:
        gdf = gpd.read_file(gpkg, **read_kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"read failed: {type(exc).__name__}: {exc}"}
    if gdf.empty:
        return {"ok": False, "error": f"no {key} features"
                + (" in the bbox" if bbox_wgs84 else "")}

    gdf.to_file(output_path, driver="GPKG", layer=layer)
    return {
        "ok": True,
        "output": str(output_path),
        "dataset": "swisstlmregio",
        "theme": key,
        "layer": layer,
        "features": int(len(gdf)),
        "crs": "EPSG:2056",
        "licence": _SWISSTOPO_LICENCE,
        "note": "swissTLMRegio (generalised ≈1:200 000) in EPSG:2056 (LV95). For "
        "full-resolution topographic detail, OSM (osm_features) is the per-bbox route.",
    }
