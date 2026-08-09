"""DGM1 connector — open 1 m terrain (DTM), the high-resolution `fetch_dem` sibling.

`fetch_dem` gives Copernicus GLO-30 (~30 m, EPSG:4326 degrees) — fine for coarse
terrain, useless for fine slope/flood or as the DTM half of a building-height
DSM−DTM. The Bundesländer publish an open **1 m DGM** (DGM1); this fetches it and
mosaics a bbox to a single GeoTIFF, in a **metric** CRS (EPSG:25832 — slope/area
work directly, no reprojection).

Like `lod2.py`, the *national* BKG DGM1 is **not** usable anonymously — its WCS
(`sg./sgx.geodatenzentrum.de/wcs_dgm1`) sits behind a `securityGate`
(registration/token required) — so the open route is the state download servers.
Wired+verified: **Bayern** (deterministic 1 km tiles) and **NRW** (1 km tiles via
the opengeodata index, because the filename carries a per-tile acquisition year).
Both are 1 m GeoTIFF, EPSG:25832. Adding a state = one `Dgm1Source`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable
from urllib.request import Request, urlopen

from chester.lod2 import _bbox_in, _grid_tiles  # shared UTM-grid math

_UA = {"User-Agent": "Mozilla/5.0 (Chester GeoAI)"}
# Cap the request so a stray country-scale bbox can't pull thousands of 1 m tiles.
_MAX_TILES = 144  # 12×12 km at 1 km tiles


@dataclass(frozen=True)
class Dgm1Source:
    """One Bundesland's open DGM1: how to turn a bbox into 1 km GeoTIFF tile URLs."""

    code: str
    name: str
    licence: str
    resolver: Callable[[list[float], str], list[tuple[str, str]]]  # (bbox, cache)→[(url,name)]
    epsg: int = 25832  # tile grid CRS (25833 for eastern states)
    mirrors: tuple[str, ...] = ()


# ── Bayern: deterministic 1 km tiles on bayernwolke ──────────────────────────

def _bayern_tiles(bbox, _cache):
    out = []
    for e, n in _grid_tiles(bbox, 1):
        name = f"{e}_{n}.tif"
        out.append((f"https://download1.bayernwolke.de/a/dgm/dgm1/{name}", name))
    return out


# ── NRW: 1 km tiles, filename carries a year → resolve via the cached index ──

_NRW_BASE = ("https://www.opengeodata.nrw.de/produkte/geobasis/hm/"
             "dgm1_tiff/dgm1_tiff/")
_NRW_INDEX = _NRW_BASE + "index.json"


def _nrw_tile_map(cache_dir: str) -> dict[str, str]:
    """{"<e>_<n>": filename} for NRW DGM1, cached (the name carries the year)."""
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, "nrw_dgm1_index.json")
    if os.path.exists(cache) and os.path.getsize(cache) > 0:
        with open(cache) as f:
            return json.load(f)
    # The product index lists sub-folders; the flat file list is the XML autoindex.
    xml = urlopen(Request(_NRW_BASE, headers=_UA), timeout=120).read().decode(
        "utf-8", "replace")
    mp: dict[str, str] = {}
    for name in re.findall(r'name="(dgm1_32_\d+_\d+_1_nw[^"]*\.tif)"', xml):
        parts = name.split("_")  # dgm1 32 <e> <n> 1 nw <year>.tif
        mp[f"{parts[2]}_{parts[3]}"] = name
    with open(cache, "w") as f:
        json.dump(mp, f)
    return mp


def _nrw_tiles(bbox, cache_dir):
    mp = _nrw_tile_map(cache_dir)
    out = []
    for e, n in _grid_tiles(bbox, 1):
        name = mp.get(f"{e}_{n}")
        if name:
            out.append((_NRW_BASE + name, name))
    return out


# ── Brandenburg: deterministic 1 km tiles (zipped GeoTIFF), UTM33 ────────────

def _brandenburg_tiles(bbox, _cache):
    base = "https://data.geobasis-bb.de/geobasis/daten/dgm/tif/"
    out = []
    for e, n in _grid_tiles(bbox, 1, epsg=25833):
        name = f"dgm_33{e}-{n}.zip"
        out.append((base + name, name))
    return out


# ── Mecklenburg-Vorpommern: 2 km GeoTIFF tiles via the Atom download endpoint ─
# The DGM feed offers several formats; the real elevation is the `gtiff` variant
# (single-band float32) served at index=4 (the shaded/coded RGB variants are NOT
# elevation). Dataset id from geodaten-mv.de/dienste/dgm_atom; refresh if changed.
_MV_DGM_DATASET = "ca268792-s2q1-4a39-b34c-9ec5bf9a4469"


def _mv_tiles(bbox, _cache):
    base = ("https://www.geodaten-mv.de/dienste/dgm_download?index=4&dataset="
            f"{_MV_DGM_DATASET}&file=")
    out = []
    for e, n in _grid_tiles(bbox, 2, epsg=25833):
        name = f"dgm1_33_{e}_{n}_2_gtiff.tif"
        out.append((base + name, name))
    return out


SOURCES: dict[str, Dgm1Source] = {
    "BY": Dgm1Source(
        "BY", "Bayern",
        "DGM1 © Bayerische Vermessungsverwaltung (DL-DE→BY 2.0)",
        resolver=_bayern_tiles,
        mirrors=("https://download1.bayernwolke.de", "https://download2.bayernwolke.de"),
    ),
    "NW": Dgm1Source(
        "NW", "Nordrhein-Westfalen",
        "DGM1 © Geobasis NRW (DL-DE/Zero 2.0)",
        resolver=_nrw_tiles,
    ),
    "BB": Dgm1Source(
        "BB", "Brandenburg",
        "DGM1 © GeoBasis-BB / LGB (DL-DE→BY 2.0)",
        resolver=_brandenburg_tiles, epsg=25833,
    ),
    "MV": Dgm1Source(
        "MV", "Mecklenburg-Vorpommern",
        "DGM1 © GeoBasis-DE/M-V (DL-DE→BY 2.0)",
        resolver=_mv_tiles, epsg=25833,
    ),
}


def _download(url: str, dest: str, mirrors: tuple[str, ...] = ()) -> bool:
    candidates = [url]
    for m in mirrors:
        for base in mirrors:
            if url.startswith(base):
                candidates.append(m + url[len(base):])
                break
    for u in dict.fromkeys(candidates):
        try:
            with urlopen(Request(u, headers=_UA), timeout=120) as r:
                if r.status != 200:
                    continue
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            return True
        except Exception:  # noqa: BLE001 - try next mirror
            continue
    return False


def _tif_from(dest: str) -> str | None:
    """A GeoTIFF path for a downloaded tile — extracting a zip if needed.

    Some states (Brandenburg) ship each 1 km tile as a ``.zip`` around the
    ``.tif``; the plain-``.tif`` states return ``dest`` unchanged.
    """
    if not dest.lower().endswith(".zip"):
        return dest
    import zipfile

    out_dir = dest + "_x"
    os.makedirs(out_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(dest) as zf:
            member = next((n for n in zf.namelist()
                           if n.lower().endswith((".tif", ".tiff"))), None)
            if member is None:
                return None
            target = os.path.join(out_dir, os.path.basename(member))
            if not (os.path.exists(target) and os.path.getsize(target) > 0):
                with zf.open(member) as fh, open(target, "wb") as f:
                    f.write(fh.read())
            return target
    except Exception:  # noqa: BLE001 - a corrupt zip = a missing tile
        return None


def detect_state(bbox: list[float], cache_dir: str) -> Dgm1Source | None:
    """Which wired state covers the bbox — probe each source's centre tile (HEAD).

    Grids don't overlap, so the first source that serves the centre tile is the
    right state. Deterministic sources (BY/BB) always name a tile — the HEAD is
    the coverage test; index sources (NW) name one only where covered.
    """
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    centre = [cx, cy, cx, cy]
    for src in SOURCES.values():
        try:
            tiles = src.resolver(centre, cache_dir)
        except Exception:  # noqa: BLE001
            continue
        if not tiles:
            continue
        try:
            req = Request(tiles[0][0], headers=_UA, method="HEAD")
            with urlopen(req, timeout=20) as r:
                if r.status == 200:
                    return src
        except Exception:  # noqa: BLE001 - not this state
            continue
    return None


def fetch_dgm1(
    bbox: list[float],
    output_path: str,
    tile_cache_dir: str,
    state: str | None = None,
) -> dict:
    """Fetch open 1 m DGM (DTM) for ``bbox`` into ``output_path`` (a GeoTIFF).

    ``bbox`` = [west, south, east, north] in WGS84. The Bundesland auto-detects
    (or pass ``state``, e.g. "BY"). Downloads the covering 1 km DGM1 tiles (cached
    in ``tile_cache_dir``), mosaics them clipped to the bbox, and writes a
    single-band GeoTIFF in **EPSG:25832 (metres, 1 m)** — ready for slope/area or
    as the DTM half of a DSM−DTM building height. Returns size, CRS and licence.
    """
    import rasterio
    from rasterio.merge import merge

    if state:
        src = SOURCES.get(state.upper())
        if src is None:
            return {"ok": False, "error": f"DGM1 not wired for '{state}'",
                    "wired": sorted(SOURCES)}
    else:
        src = detect_state(bbox, tile_cache_dir)
        if src is None:
            wired = ", ".join(f"{s.name} ({s.code})" for s in SOURCES.values())
            return {"ok": False, "error": "bbox is not covered by a wired open DGM1 "
                    f"source (wired: {wired}). BKG's nationwide DGM1 needs a "
                    "registered token (securityGate); other Bundesländer are not "
                    "yet wired.", "bbox": bbox}

    tiles = src.resolver(bbox, tile_cache_dir)
    if not tiles:
        return {"ok": False, "error": "no DGM1 tiles cover the bbox", "state": src.code}
    if len(tiles) > _MAX_TILES:
        return {"ok": False, "error": f"bbox needs {len(tiles)} 1 m tiles (> "
                f"{_MAX_TILES}); narrow it — DGM1 at 1 m is heavy.", "state": src.code}

    os.makedirs(tile_cache_dir, exist_ok=True)
    paths, missing = [], []
    for url, name in tiles:
        dest = os.path.join(tile_cache_dir, name)
        if not (os.path.exists(dest) and os.path.getsize(dest) > 0):
            if not _download(url, dest, src.mirrors):
                missing.append(name)
                continue
        tif = _tif_from(dest)
        if tif is None:
            missing.append(f"{name} (no tif in zip)")
            continue
        paths.append(tif)
    if not paths:
        return {"ok": False, "error": "no DGM1 tiles could be downloaded",
                "state": src.code, "tiles_missing": missing}

    minx, miny, maxx, maxy = _bbox_in(bbox, src.epsg)
    datasets = [rasterio.open(p) for p in paths]
    try:
        mosaic, transform = merge(datasets, bounds=(minx, miny, maxx, maxy))
        profile = datasets[0].profile
        crs = str(datasets[0].crs)
    finally:
        for ds in datasets:
            ds.close()
    profile.update(driver="GTiff", count=1, height=mosaic.shape[1],
                   width=mosaic.shape[2], transform=transform, compress="lzw")
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(mosaic[0], 1)

    return {
        "ok": True,
        "output": output_path,
        "state": src.code,
        "state_name": src.name,
        "size": [int(mosaic.shape[2]), int(mosaic.shape[1])],
        "resolution_m": 1,
        "crs": crs or f"EPSG:{src.epsg}",
        "tiles_used": len(paths),
        "tiles_missing": missing,
        "licence": src.licence,
        "note": f"1 m DGM in EPSG:{src.epsg} (metres) — slope/area work directly; "
        "no reprojection needed (unlike fetch_dem's GLO-30).",
    }
