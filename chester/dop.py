"""DOP connector — open aerial orthophotos, the imagery sibling of `fetch_dgm1`.

Chester could already fetch terrain (DGM1), buildings (LoD2), boundaries,
statistics and transit — but no **aerial image as a dataset**. The only image
path was `fetch_wms_map`, and that one is explicitly barred from analysis (a
rendered, symbolised picture with no defined radiometry and no band assignment).
This closes that gap: the Bundesländer's open DOP, tiled, mosaicked to a bbox and
written as a GeoTIFF in a **metric** CRS (EPSG:25832/25833).

The payoff beyond "a nicer basemap" is the **fourth band**: most wired sources ship
**RGBI** (with near infrared), so the existing `spectral_index` tool computes NDVI at
10–20 cm instead of Sentinel-2's 10 m — tree crowns, backyard vegetation, sealed
surface per parcel — with no new perception code. Bayern is the exception (RGB only,
see below), so check `has_nir` in the result before planning an NDVI step.

Same Länder-open story as `lod2.py`/`dgm1.py`. Wired+verified: **NRW** (10 cm RGBI
JPEG2000, 1 km tiles, year in the filename → index lookup), **Brandenburg** (20 cm
RGBI, zipped 1 km tiles, UTM33), **Mecklenburg-Vorpommern** (20 cm RGBI, 2 km tiles
via the Atom endpoint) and **Bayern** (20 cm **RGB**, 1 km tiles).

Bayern needed one extra step: unlike the DGM1 its tile URL is not guessable from the
DGM1 layout (an extra `data/` path segment and a UTM-zone prefix in the filename).
The layout was read off the per-Gemeinde metalinks the portal itself serves at
`geodaten.bayern.de/odd/a/dop20/meta/metalink/<AGS>.meta4`; once known, tiles derive
deterministically like the other states. Bayern *also* publishes a CIR (infrared)
DOP20, but only through a polygon→metalink service with no derivable per-tile URL —
so Bavarian NDVI is not available here.

DOP tiles are heavy (33–83 MB each, against ~2 MB for a 1 km DGM1 tile), so
`_MAX_TILES` is far stricter here than in `dgm1.py`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable
from urllib.request import Request, urlopen

from chester.dgm1 import _download, _tif_from  # shared download + zip unwrap
from chester.lod2 import _bbox_in, _grid_tiles  # shared UTM-grid math

_UA = {"User-Agent": "Mozilla/5.0 (Chester GeoAI)"}
# Cap hard: one DOP tile is 18 MB (BY dop40) to 91 MB (BB zip). 16 tiles is already
# well over a gigabyte — past what a chat turn should download.
_MAX_TILES = 16
# `dgm1._download`'s 120 s default is sized for ~2 MB DGM1 tiles and silently turns
# a healthy 91 MB DOP tile into a "missing tile" (observed on Brandenburg).
_TILE_TIMEOUT = 600


@dataclass(frozen=True)
class DopSource:
    """One Bundesland's open DOP: how to turn a bbox into image tile URLs."""

    code: str
    name: str
    licence: str
    resolution_m: float
    bands: str = "RGBI"  # RGBI = with near infrared → NDVI works
    tile_km: int = 1
    epsg: int = 25832  # tile grid CRS (25833 for eastern states)
    resolver: Callable[[list[float], str], list[tuple[str, str]]] | None = None
    status: str = "open"  # "open" = wired, "documented" = open but not fetchable
    portal: str = ""  # where a human can get it when status != "open"
    mirrors: tuple[str, ...] = ()


# ── NRW: 1 km JPEG2000 tiles, filename carries a year → resolve via the index ──
# Same shape as the NRW DGM1 source: the acquisition year is part of the name, so
# a tile name cannot be derived, only looked up (and then cached).

_NW_BASE = ("https://www.opengeodata.nrw.de/produkte/geobasis/lusat/akt/dop/"
            "dop_jp2_f10/")


def _nw_tile_map(cache_dir: str) -> dict[str, str]:
    """{"<e>_<n>": filename} for NRW DOP10, cached (the name carries the year)."""
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, "nw_dop10_index.json")
    if os.path.exists(cache) and os.path.getsize(cache) > 0:
        with open(cache) as f:
            return json.load(f)
    xml = urlopen(Request(_NW_BASE, headers=_UA), timeout=180).read().decode(
        "utf-8", "replace")
    mp: dict[str, str] = {}
    for name in re.findall(r'name="(dop10rgbi_32_\d+_\d+_1_nw[^"]*\.jp2)"', xml):
        parts = name.split("_")  # dop10rgbi 32 <e> <n> 1 nw <year>.jp2
        mp[f"{parts[2]}_{parts[3]}"] = name
    with open(cache, "w") as f:
        json.dump(mp, f)
    return mp


def _nw_tiles(bbox, cache_dir):
    mp = _nw_tile_map(cache_dir)
    out = []
    for e, n in _grid_tiles(bbox, 1):
        name = mp.get(f"{e}_{n}")
        if name:
            out.append((_NW_BASE + name, name))
    return out


# ── Bayern: deterministic 1 km tiles, but a different layout than the DGM1 ────
# Two traps that make this *not* a copy of `dgm1._bayern_tiles`: the path carries
# an extra `data/` segment, and the filename is prefixed with the UTM zone
# (`32726_5433.tif`, not `726_5433.tif`). Derived from the per-Gemeinde metalinks
# under geodaten.bayern.de/odd/a/dop20/meta/metalink/<AGS>.meta4, which is also
# where a human lands from the portal.
# RGB only: Bayern *does* publish a CIR (infrared) DOP20, but it is offered solely
# through the polygon→metalink service, with no derivable per-tile URL — so NDVI
# is not available here, unlike NW/BB/MV.

def _by_tiles(bbox, _cache):
    out = []
    for e, n in _grid_tiles(bbox, 1):
        name = f"32{e}_{n}.tif"
        out.append((f"https://download1.bayernwolke.de/a/dop20/data/{name}", name))
    return out


# ── Brandenburg: deterministic 1 km tiles (zipped GeoTIFF), UTM33 ─────────────

def _bb_tiles(bbox, _cache):
    base = "https://data.geobasis-bb.de/geobasis/daten/dop/rgbi_tif/"
    out = []
    for e, n in _grid_tiles(bbox, 1, epsg=25833):
        name = f"dop_33{e}-{n}.zip"
        out.append((base + name, name))
    return out


# ── Mecklenburg-Vorpommern: 2 km tiles via the Atom download endpoint ─────────
# The DOP20 feed carries an RGBI and an RGB dataset; this is the **RGBI** one
# (dataset id from geodaten-mv.de/dienste/dop20_atom; refresh if it changes).
_MV_DOP_DATASET = "f94d17fa-b29b-41f7-a4b8-6e10f1aae38e"


def _mv_tiles(bbox, _cache):
    base = ("https://www.geodaten-mv.de/dienste/dop20_download?index=0&dataset="
            f"{_MV_DOP_DATASET}&file=")
    out = []
    for e, n in _grid_tiles(bbox, 2, epsg=25833):
        name = f"dop20rgbi_33_{e}_{n}_2_mv.tif"
        out.append((base + name, name))
    return out


SOURCES: dict[str, DopSource] = {
    "NW": DopSource(
        "NW", "Nordrhein-Westfalen",
        "DOP10 © Geobasis NRW (DL-DE/Zero 2.0)",
        resolution_m=0.1, bands="RGBI", tile_km=1, epsg=25832,
        resolver=_nw_tiles,
    ),
    "BB": DopSource(
        "BB", "Brandenburg",
        "DOP20 © GeoBasis-BB / LGB (DL-DE→BY 2.0)",
        resolution_m=0.2, bands="RGBI", tile_km=1, epsg=25833,
        resolver=_bb_tiles,
    ),
    "MV": DopSource(
        "MV", "Mecklenburg-Vorpommern",
        "DOP20 © GeoBasis-DE/M-V (CC BY 4.0)",
        resolution_m=0.2, bands="RGBI", tile_km=2, epsg=25833,
        resolver=_mv_tiles,
    ),
    "BY": DopSource(
        "BY", "Bayern",
        "DOP20 © Bayerische Vermessungsverwaltung (CC BY 4.0)",
        resolution_m=0.2, bands="RGB", tile_km=1, epsg=25832,
        resolver=_by_tiles,
        mirrors=("https://download1.bayernwolke.de", "https://download2.bayernwolke.de"),
    ),
}


# ── aerial backdrop (WMS) ────────────────────────────────────────────────────
# A *backdrop* wants a picture, not data: one GetMap is 70 KB in 0.3 s, against
# 18-91 MB for a single data tile. So the visual check and the 3D ground plate use
# these services, while `fetch_dop` stays the route for anything to be analysed.
# CRS:84 is lon/lat order, which matches Chester's WGS84 bbox convention.
WMS_BACKDROPS: dict[str, tuple[str, str]] = {
    "BY": ("https://geoservices.bayern.de/od/wms/dop/v1/dop20", "by_dop20c"),
    "NW": ("https://www.wms.nrw.de/geobasis/wms_nw_dop", "nw_dop_rgb"),
}


def aerial_backdrop_png(bbox: list[float], width: int = 900,
                        height: int = 700) -> bytes | None:
    """Aerial imagery covering ``bbox`` as PNG/JPEG bytes, or ``None``.

    Best-effort by design: no coverage, no network, a slow service — all yield
    ``None`` and the caller falls back to OSM. Tries each registered service and
    takes the first that answers with an image.
    """
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    w, s, e, n = bbox
    for url, layer in WMS_BACKDROPS.values():
        query = urlencode({
            "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
            "LAYERS": layer, "STYLES": "", "CRS": "CRS:84",
            "BBOX": f"{w},{s},{e},{n}", "WIDTH": width, "HEIGHT": height,
            "FORMAT": "image/jpeg",
        })
        try:
            with urlopen(Request(f"{url}?{query}", headers=_UA), timeout=30) as r:
                if r.status != 200 or "image" not in r.headers.get("Content-Type", ""):
                    continue
                data = r.read()
            # A service outside its area answers 200 with a blank tile; a few KB of
            # uniform JPEG is the tell.
            if len(data) > 4000:
                return data
        except Exception:  # noqa: BLE001 - try the next service
            continue
    return None


def acquisition_years(tile_names: list[str]) -> list[int]:
    """Capture years read out of the tile filenames, where the source states one.

    Only NRW puts the flight year in the name (``dop10rgbi_32_280_5652_1_nw_2025``).
    BY/BB/MV do not, so this returns an empty list for them rather than guessing —
    an invented year is worse than a missing one, because it looks like knowledge.
    """
    years = {int(m) for name in tile_names
             for m in re.findall(r"_((?:19|20)\d{2})[._]", name)}
    return sorted(years)


def dop_sources() -> list[dict]:
    """Every registered DOP source with its status — wired or merely documented."""
    return [
        {
            "state": s.code,
            "name": s.name,
            "status": s.status,
            "resolution_m": s.resolution_m,
            "bands": s.bands,
            "has_nir": "I" in s.bands,
            "crs": f"EPSG:{s.epsg}",
            "tile_km": s.tile_km,
            "licence": s.licence,
            **({"portal": s.portal} if s.portal else {}),
        }
        for s in SOURCES.values()
    ]


def detect_state(bbox: list[float], cache_dir: str) -> DopSource | None:
    """Which wired state covers the bbox — probe each source's centre tile (HEAD).

    Grids don't overlap, so the first source serving the centre tile is the right
    state. `documented` sources have no resolver and are skipped.
    """
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    centre = [cx, cy, cx, cy]
    for src in SOURCES.values():
        if src.status != "open" or src.resolver is None:
            continue
        try:
            tiles = src.resolver(centre, cache_dir)
        except Exception:  # noqa: BLE001 - a broken index is not this state
            continue
        if not tiles:
            continue
        try:
            req = Request(tiles[0][0], headers=_UA, method="HEAD")
            with urlopen(req, timeout=30) as r:
                if r.status == 200:
                    return src
        except Exception:  # noqa: BLE001 - not this state
            continue
    return None


def fetch_dop(  # noqa: C901
# C901-Ausnahme: wie fetch_lod2: Landeserkennung, Kachelkappe, fehlende Kacheln, documented-Quellen
    bbox: list[float],
    output_path: str,
    tile_cache_dir: str,
    state: str | None = None,
) -> dict:
    """Fetch an open aerial orthophoto (DOP) for ``bbox`` into ``output_path``.

    ``bbox`` = [west, south, east, north] in WGS84. The Bundesland auto-detects
    (or pass ``state``, e.g. "NW"). Downloads the covering DOP tiles (cached in
    ``tile_cache_dir``), mosaics them clipped to the bbox and writes a **multi-band
    GeoTIFF** in a metric CRS (EPSG:25832/25833) — keeping every band, so an RGBI
    source stays NDVI-capable. Returns size, band count, resolution and licence.
    """
    import rasterio
    from rasterio.merge import merge

    if state:
        src = SOURCES.get(state.upper())
        if src is None:
            return {"ok": False, "error": f"DOP not registered for '{state}'",
                    "wired": sorted(s.code for s in SOURCES.values()
                                    if s.status == "open")}
        if src.status != "open":
            return {"ok": False, "error": f"{src.name} publishes its DOP openly but "
                    "not at a stable per-tile URL (portal/SPA download only), so it "
                    "cannot be fetched automatically.", "portal": src.portal,
                    "licence": src.licence}
    else:
        src = detect_state(bbox, tile_cache_dir)
        if src is None:
            wired = ", ".join(f"{s.name} ({s.code})" for s in SOURCES.values()
                              if s.status == "open")
            other = ", ".join(f"{s.name} ({s.code})" for s in SOURCES.values()
                              if s.status != "open")
            return {"ok": False, "error": "bbox is not covered by a wired open DOP "
                    f"source (wired: {wired}). Documented but not fetchable: {other}. "
                    "Elsewhere, use fetch_wms_map for a picture — but note it is a "
                    "rendered image, not analysable data.", "bbox": bbox}

    assert src.resolver is not None  # guaranteed by status == "open"
    tiles = src.resolver(bbox, tile_cache_dir)
    if not tiles:
        return {"ok": False, "error": "no DOP tiles cover the bbox", "state": src.code}
    if len(tiles) > _MAX_TILES:
        return {"ok": False, "error": f"bbox needs {len(tiles)} DOP tiles (> "
                f"{_MAX_TILES}); narrow it — one tile is 30-80 MB at "
                f"{src.resolution_m} m.", "state": src.code}

    os.makedirs(tile_cache_dir, exist_ok=True)
    paths, missing = [], []
    for url, name in tiles:
        dest = os.path.join(tile_cache_dir, name)
        if not (os.path.exists(dest) and os.path.getsize(dest) > 0):
            if not _download(url, dest, src.mirrors, timeout=_TILE_TIMEOUT):
                missing.append(name)
                continue
        img = _tif_from(dest)  # unwraps a zipped tile, passes .tif/.jp2 through
        if img is None:
            missing.append(f"{name} (no image in zip)")
            continue
        paths.append(img)
    if not paths:
        return {"ok": False, "error": "no DOP tiles could be downloaded",
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
    bands = int(mosaic.shape[0])
    # Always write GTiff — the source may be JPEG2000 (NRW), which we do not keep.
    profile.update(driver="GTiff", count=bands, height=mosaic.shape[1],
                   width=mosaic.shape[2], transform=transform, compress="deflate")
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(mosaic)

    years = acquisition_years([name for _u, name in tiles])
    has_nir = bands >= 4
    return {
        "ok": True,
        "output": output_path,
        "state": src.code,
        "state_name": src.name,
        "size": [int(mosaic.shape[2]), int(mosaic.shape[1])],
        "bands": bands,
        "has_nir": has_nir,
        "resolution_m": src.resolution_m,
        "crs": crs or f"EPSG:{src.epsg}",
        "tiles_used": len(paths),
        "tiles_missing": missing,
        "licence": src.licence,
        # Empty where the source does not state it (BY/BB/MV) — see acquisition_years.
        "acquired_years": years,
        "acquired": (str(years[0]) if len(years) == 1
                     else f"{years[0]}-{years[-1]}" if years else None),
        "note": (
            f"{src.resolution_m} m orthophoto in EPSG:{src.epsg} (metres), {bands} bands"
            + (" incl. near infrared — band 4 is NIR, so spectral_index can compute "
               "NDVI at this resolution." if has_nir else ".")
            + " This is image DATA (defined radiometry), unlike fetch_wms_map's "
              "rendered picture."
        ),
    }
