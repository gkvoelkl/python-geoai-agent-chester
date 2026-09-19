"""Administrative-boundaries connector — official German/EU boundary polygons.

The missing piece for **official-statistics → choropleth**: Chester's statistics
connectors (Eurostat / Wikidata / World Bank) deliver *tables* keyed by AGS / NUTS
code, but a choropleth needs the matching *geometry*. This connector fetches the
authoritative boundary polygons from the BKG's open **Verwaltungsgebiete** — one
national GeoPackage each, DL-DE→BY 2.0 (free, attribution) — carrying exactly
those keys, so the table joins straight onto the polygons (a normal
`native:joinattributestable` step — no bespoke join tool, per the statistics
design).

Two datasets, one registry each (this is the pure core, no SelmaKit dep, like
``lod2.py`` / ``geocache.py``):

- **vg250** (Verwaltungsgebiete 1:250 000) — German admin units at five levels,
  keyed by **AGS** (Amtlicher Gemeindeschlüssel): Staat / Land / Regierungsbezirk /
  Kreis / Gemeinde. The ``AGS`` column is the Wikidata connector's join key, and
  its prefix is the ``adminlevels.region_hierarchy`` chain.
- **nuts250** (NUTS 1:250 000) — EU statistical regions at NUTS-1/2/3, keyed by
  **NUTS_CODE** (the Eurostat ``geo`` key).

Each source is a ~12–72 MB national GeoPackage (inside a zip); it is downloaded
and cached **once** under an internal ``_boundaries`` cache dir (``_``-prefixed, so
the GeoCache inventory scan skips it), then subset per request (by admin level,
an AGS/NUTS/name ``match``, and/or a bbox). ``GF`` (Geofaktor) = 4 selects the
land-with-structure polygons, dropping the water-body variants — the sane default.
"""

from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass
from urllib.request import Request, urlopen

_UA = {"User-Agent": "Mozilla/5.0 (Chester Geo-AI)"}
_BKG_LICENCE = ("© GeoBasis-DE / BKG (Verwaltungsgebiete 1:250 000, "
                "DL-DE→BY 2.0) — dl-de/by-2-0")


@dataclass(frozen=True)
class BoundarySource:
    """One BKG boundary dataset: its national-GPKG zip, layers and key columns."""

    dataset: str                 # "vg250" | "nuts250"
    zip_url: str
    cache_name: str              # cached .gpkg filename
    levels: dict[str, str]       # level code → layer name
    key_col: str                 # the join key (AGS / NUTS_CODE)
    name_col: str
    keep_cols: tuple[str, ...]   # attribute columns to keep in the subset


VG250 = BoundarySource(
    dataset="vg250",
    zip_url=("https://daten.gdz.bkg.bund.de/produkte/vg/vg250_ebenen_0101/"
             "aktuell/vg250_01-01.utm32s.gpkg.ebenen.zip"),
    cache_name="vg250.gpkg",
    levels={
        "STA": "vg250_sta",   # Staat (Bund)
        "LAN": "vg250_lan",   # Länder
        "RBZ": "vg250_rbz",   # Regierungsbezirke
        "KRS": "vg250_krs",   # Kreise / kreisfreie Städte
        "VWG": "vg250_vwg",   # Verwaltungsgemeinschaften
        "GEM": "vg250_gem",   # Gemeinden
    },
    key_col="AGS",
    name_col="GEN",
    keep_cols=("AGS", "GEN", "BEZ", "NUTS"),
)

NUTS250 = BoundarySource(
    dataset="nuts250",
    zip_url=("https://daten.gdz.bkg.bund.de/produkte/vg/nuts250_1231/"
             "aktuell/nuts250_12-31.utm32s.gpkg.zip"),
    cache_name="nuts250.gpkg",
    levels={
        "NUTS1": "nuts250_n1",
        "NUTS2": "nuts250_n2",
        "NUTS3": "nuts250_n3",
    },
    key_col="NUTS_CODE",
    name_col="NUTS_NAME",
    keep_cols=("NUTS_CODE", "NUTS_NAME", "NUTS_LEVEL"),
)

SOURCES = {VG250.dataset: VG250, NUTS250.dataset: NUTS250}

# level code → source, so a caller names just "GEM" or "NUTS3".
_LEVEL_TO_SOURCE = {lvl: src for src in SOURCES.values() for lvl in src.levels}


def levels_catalog() -> list[dict]:
    """Every fetchable level: dataset, level code, admin name and join key."""
    label = {
        "STA": "Staat (Bund)", "LAN": "Land", "RBZ": "Regierungsbezirk",
        "KRS": "Kreis / kreisfreie Stadt", "VWG": "Verwaltungsgemeinschaft",
        "GEM": "Gemeinde", "NUTS1": "NUTS-1 (Länder-Gruppen)",
        "NUTS2": "NUTS-2 (Regierungsbezirke)", "NUTS3": "NUTS-3 (Kreise)",
    }
    out = []
    for lvl, src in _LEVEL_TO_SOURCE.items():
        out.append({"level": lvl, "dataset": src.dataset,
                    "admin": label.get(lvl, lvl), "key": src.key_col})
    return out


def _ensure_gpkg(src: BoundarySource, cache_dir: str) -> str:
    """Download+unzip the national GeoPackage once; return the cached path."""
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, src.cache_name)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    with urlopen(Request(src.zip_url, headers=_UA), timeout=300) as r:
        blob = r.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        member = next((n for n in zf.namelist() if n.lower().endswith(".gpkg")), None)
        if member is None:
            raise RuntimeError(f"no .gpkg inside {src.zip_url}")
        with zf.open(member) as fh, open(dest, "wb") as out:
            out.write(fh.read())
    return dest


def _bbox_to_utm32(bbox_wgs84: list[float]):
    from pyproj import Transformer

    tr = Transformer.from_crs(4326, 25832, always_xy=True)
    _w, _s, _e, _n = bbox_wgs84
    return tuple(tr.transform_bounds(_w, _s, _e, _n))


def fetch_boundaries(
    level: str,
    output_path: str,
    cache_dir: str,
    match: str | None = None,
    bbox: list[float] | None = None,
    land_only: bool = True,
) -> dict:
    """Fetch official boundary polygons for ``level`` into ``output_path`` (GPKG).

    ``level`` is one of STA/LAN/RBZ/KRS/VWG/GEM (German, keyed by ``AGS``) or
    NUTS1/NUTS2/NUTS3 (EU, keyed by ``NUTS_CODE``). ``match`` filters by key prefix
    (e.g. "09" = Bavaria, "09162" = München Kreis) **or** name substring
    ("München"). ``bbox`` = [w,s,e,n] WGS84 spatially windows the result.
    ``land_only`` keeps the GF=4 land polygons (drops water-body variants). The
    output carries the join key so a statistics table joins straight onto it.
    """
    import geopandas as gpd

    lvl = level.strip().upper()
    src = _LEVEL_TO_SOURCE.get(lvl)
    if src is None:
        return {"ok": False, "error": f"unknown level '{level}'",
                "known": sorted(_LEVEL_TO_SOURCE)}
    try:
        gpkg = _ensure_gpkg(src, cache_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"download failed: {type(exc).__name__}: {exc}"}

    read_kwargs = {"layer": src.levels[lvl]}
    if bbox:
        read_kwargs["bbox"] = _bbox_to_utm32(bbox)
    try:
        gdf = gpd.read_file(gpkg, **read_kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"read failed: {type(exc).__name__}: {exc}"}

    if land_only and "GF" in gdf.columns:
        gdf = gdf[gdf["GF"] == 4]

    if match:
        m = match.strip()
        key = gdf[src.key_col].astype(str)
        name = gdf[src.name_col].astype(str).str.lower()
        gdf = gdf[key.str.startswith(m) | name.str.contains(m.lower(), regex=False)]

    if gdf.empty:
        return {"ok": False, "error": f"no {lvl} units matched"
                + (f" '{match}'" if match else "")
                + (" in the bbox" if bbox else "")}

    keep = [c for c in src.keep_cols if c in gdf.columns] + ["geometry"]
    gdf = gdf[keep]
    gdf.to_file(output_path, driver="GPKG", layer=src.levels[lvl])

    return {
        "ok": True,
        "output": output_path,
        "dataset": src.dataset,
        "level": lvl,
        "key_column": src.key_col,
        "units": int(len(gdf)),
        "crs": "EPSG:25832",
        "licence": _BKG_LICENCE,
    }
