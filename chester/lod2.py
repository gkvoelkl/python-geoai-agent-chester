"""LoD2 building-model connector — authoritative German building heights.

The answer to "real building heights": the federal states (Bundesländer) publish
**LoD2 3D building models** as open data. Every building carries a laser-measured
``bldg:measuredHeight`` (ALS-derived, mm precision) — this is the true building
height, and it is *superior* to DSM−DTM raster differencing (no vegetation
contamination, no edge/resampling artefacts, per building, already summarised).
Copernicus GLO-30 (``fetch_dem``) is ~30 m and useless for individual buildings;
the BKG's nationwide LoD2-DE exists but is licence-restricted to public
authorities (V GeoBund). The open route is the state portals.

This module is the pure core (no SelmaKit / pydantic-ai dependency, like
``geocache.py``): a per-state **registry** of open LoD2 sources, deterministic
**tile derivation** from a WGS84 bbox, a streaming **CityGML → GeoDataFrame**
parser (footprint + measured height + address), and the ``fetch`` orchestration.
Each state is one registry entry; adding a state = adding a ``StateSource`` (and,
for the not-yet-wired ones, a tile resolver). The capability in
``chester/capabilities/lod2.py`` is the thin agent layer over this.

CityGML specifics (verified on Bayern + NRW tiles): planar CRS is
ETRS89 / UTM32 = **EPSG:25832** (metric — heights and areas are already in
metres); the footprint is the ``bldg:GroundSurface`` exterior ring (3D coords,
Z dropped); ``bldg:measuredHeight`` is per ``bldg:Building`` (building parts may
carry their own — we take the building envelope = the max).
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable
from urllib.request import Request, urlopen

# ── per-state registry ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class StateSource:
    """One Bundesland's open LoD2 source.

    ``status`` is ``"open"`` for a wired, verified tile resolver, or
    ``"documented"`` for a state whose open LoD2 exists but whose portal-specific
    access (Atom feed / WFS / interactive download centre) is not yet wired — the
    registry still advertises it truthfully so the agent can point the user there.
    ``resolver`` maps a WGS84 bbox to a list of ``(url, tile_name)`` CityGML tiles;
    ``None`` for documented-only states.
    """

    code: str          # ISO-3166-2 subdivision (e.g. "BY")
    name: str
    epsg: int          # planar CRS of the tiles (25832 = ETRS89/UTM32 everywhere)
    licence: str
    status: str        # "open" | "documented" | "restricted"
    portal: str        # human-facing portal URL (for documented states / attribution)
    resolver: Callable[[list[float]], list[tuple[str, str]]] | None = None
    mirrors: tuple[str, ...] = field(default_factory=tuple)


def _bbox_in(bbox_wgs84: list[float], epsg: int) -> tuple[float, float, float, float]:
    """WGS84 [w,s,e,n] → (minx,miny,maxx,maxy) in EPSG:``epsg`` (metres)."""
    from pyproj import Transformer

    tr = Transformer.from_crs(4326, epsg, always_xy=True)
    _w, _s, _e, _n = bbox_wgs84
    return tr.transform_bounds(_w, _s, _e, _n)


def _grid_tiles(bbox_wgs84: list[float], step_km: int,
                epsg: int = 25832) -> list[tuple[int, int]]:
    """Lower-left corners (km) of the ``step_km`` grid tiles covering bbox in ``epsg``.

    ``epsg`` is the tile grid's CRS — EPSG:25832 (UTM32) for western states,
    25833 (UTM33) for the eastern ones (Brandenburg/Berlin/Sachsen/M-V).
    """
    minx, miny, maxx, maxy = _bbox_in(bbox_wgs84, epsg)
    e0 = int(minx // (step_km * 1000)) * step_km
    e1 = int(maxx // (step_km * 1000)) * step_km
    n0 = int(miny // (step_km * 1000)) * step_km
    n1 = int(maxy // (step_km * 1000)) * step_km
    return [(e, n)
            for e in range(e0, e1 + 1, step_km)
            for n in range(n0, n1 + 1, step_km)]


def _bayern_tiles(bbox_wgs84: list[float]) -> list[tuple[str, str]]:
    # 2 km CityGML tiles: https://download1.bayernwolke.de/a/lod2/citygml/<E>_<N>.gml
    out = []
    for e, n in _grid_tiles(bbox_wgs84, 2):
        name = f"{e}_{n}.gml"
        out.append((f"https://download1.bayernwolke.de/a/lod2/citygml/{name}", name))
    return out


def _nrw_tiles(bbox_wgs84: list[float]) -> list[tuple[str, str]]:
    # 1 km CityGML tiles: .../lod2_gml/lod2_gml/LoD2_32_<E>_<N>_1_NW.gml
    base = ("https://www.opengeodata.nrw.de/produkte/geobasis/3dg/"
            "lod2_gml/lod2_gml/")
    out = []
    for e, n in _grid_tiles(bbox_wgs84, 1):
        name = f"LoD2_32_{e}_{n}_1_NW.gml"
        out.append((base + name, name))
    return out


def _brandenburg_tiles(bbox_wgs84: list[float]) -> list[tuple[str, str]]:
    # 1 km CityGML tiles, zipped, UTM33: .../lod2_gml/lod2_33<E>-<N>.zip
    base = "https://data.geobasis-bb.de/geobasis/daten/3d_gebaeude/lod2_gml/"
    out = []
    for e, n in _grid_tiles(bbox_wgs84, 1, epsg=25833):
        name = f"lod2_33{e}-{n}.zip"
        out.append((base + name, name))
    return out


# Mecklenburg-Vorpommern serves LoD2 via an INSPIRE Atom feed, but the tile names
# are deterministic (2 km CityGML zips, UTM33) and the download endpoint takes a
# fixed dataset id — so we derive tile URLs directly (from
# geodaten-mv.de/dienste/gebaeude_atom). If the id ever changes, refresh it there.
_MV_LOD2_DATASET = "8397b554-5cb9-4274-8be8-c20490d9a6e8"


def _mv_tiles(bbox_wgs84: list[float]) -> list[tuple[str, str]]:
    base = ("https://www.geodaten-mv.de/dienste/gebaeude_download?index=0&dataset="
            f"{_MV_LOD2_DATASET}&file=")
    out = []
    for e, n in _grid_tiles(bbox_wgs84, 2, epsg=25833):
        name = f"lod2_33_{e}_{n}_2_gml.zip"
        out.append((base + name, name))
    return out


# One entry per Bundesland with open LoD2. Wired+verified: BY, NW, BB, MV — the
# states that expose a clean programmatic endpoint (a deterministic tile server or
# a standard INSPIRE Atom feed with derivable tile names on a stable host). The
# others publish LoD2 openly too (DL-DE/Zero 2.0) but only behind portal-specific
# access we cannot fetch per-bbox, so they stay "documented" (honest, not guessed).
# Investigated and found NOT cleanly wireable (as of 2026-07):
#   NI — opengeodata is an ArcGIS-Hub SPA (no derivable tile URLs)
#   TH — portal restructured; old dladownload PHP endpoints are gone (404)
#   SH — public access is a gaialight JS download-client (DANord), no Atom
#   BE — FIS-Broker Atom 403/404; businesslocationcenter 3D portal is an SPA
#   ST — LoD2 is only whole-state bulk ZIPs; the DGM1 WCS OpenData 500s on GetCoverage
#   HE — interactive gds.hessen.de downloadcenter (POST-based JS), no Atom/WCS/static
#   HB — no discoverable programmatic LoD2/DGM endpoint
# Re-check periodically; several run modernisation programmes.
BUNDESLAENDER: dict[str, StateSource] = {
    "BY": StateSource(
        "BY", "Bayern", 25832,
        "3D-Gebäudemodell LoD2 © Bayerische Vermessungsverwaltung (DL-DE→BY 2.0)",
        "open", "https://geodaten.bayern.de",
        resolver=_bayern_tiles,
        mirrors=("https://download1.bayernwolke.de", "https://download2.bayernwolke.de"),
    ),
    "NW": StateSource(
        "NW", "Nordrhein-Westfalen", 25832,
        "3D-Gebäudemodell LoD2 © Geobasis NRW (DL-DE/Zero 2.0)",
        "open", "https://www.opengeodata.nrw.de",
        resolver=_nrw_tiles,
    ),
    "TH": StateSource("TH", "Thüringen", 25832,
        "LoD2 © GDI-Th / TLBG (DL-DE/Zero 2.0)", "documented",
        "https://www.geoportal-th.de"),
    "HE": StateSource("HE", "Hessen", 25832,
        "LoD2 © HVBG (DL-DE/BY 2.0)", "documented", "https://gds.hessen.de"),
    "BB": StateSource(
        "BB", "Brandenburg", 25833,
        "3D-Gebäudemodell LoD2 © GeoBasis-BB / LGB (DL-DE→BY 2.0)",
        "open", "https://data.geobasis-bb.de", resolver=_brandenburg_tiles),
    "BE": StateSource("BE", "Berlin", 25833,
        "LoD2 © Geoportal Berlin (DL-DE/BY 2.0)", "documented",
        "https://www.businesslocationcenter.de/downloadportal"),
    "HH": StateSource("HH", "Hamburg", 25832,
        "LoD2 © LGV Hamburg (DL-DE/BY 2.0)", "documented",
        "https://metaver.de"),
    "SN": StateSource("SN", "Sachsen", 25833,
        "LoD2 © GeoSN (DL-DE/BY 2.0)", "documented",
        "https://www.landesvermessung.sachsen.de"),
    "ST": StateSource("ST", "Sachsen-Anhalt", 25832,
        "LoD2 © LVermGeo LSA (DL-DE/BY 2.0)", "documented",
        "https://www.lvermgeo.sachsen-anhalt.de"),
    "SH": StateSource("SH", "Schleswig-Holstein", 25832,
        "LoD2 © LVermGeo SH (DL-DE/Zero 2.0)", "documented",
        # No clean Atom — public access is a gaialight JS download-client.
        "https://geodaten.schleswig-holstein.de/gaialight-sh/_apps/dladownload/"),
    "NI": StateSource("NI", "Niedersachsen", 25832,
        "LoD2 © LGLN (DL-DE/Zero 2.0)", "documented",
        "https://opengeodata.lgln.niedersachsen.de"),
    "MV": StateSource(
        "MV", "Mecklenburg-Vorpommern", 25833,
        "3D-Gebäudemodell LoD2 © GeoBasis-DE/M-V (DL-DE→BY 2.0)",
        "open", "https://www.geodaten-mv.de", resolver=_mv_tiles),
    "RP": StateSource("RP", "Rheinland-Pfalz", 25832,
        "LoD2 © LVermGeo RLP (DL-DE/BY 2.0)", "documented",
        "https://lvermgeo.rlp.de"),
    "SL": StateSource("SL", "Saarland", 25832,
        "LoD2 © LVGL Saarland (DL-DE/BY 2.0)", "documented",
        "https://geoportal.saarland.de"),
    "HB": StateSource("HB", "Bremen", 25832,
        "LoD2 © GeoInformation Bremen (DL-DE/BY 2.0)", "documented",
        "https://www.geo.bremen.de"),
    # Baden-Württemberg (BW) is deliberately absent: LGL BW does not publish LoD2
    # as open data (fee-based), so there is no free source to wire.
}


def open_states() -> list[StateSource]:
    return [s for s in BUNDESLAENDER.values() if s.status == "open"]


# ── CityGML → GeoDataFrame (streaming) ───────────────────────────────────────


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ring_xy(pos_text: str) -> list[tuple[float, float]]:
    """A gml:posList body → [(x, y), …], dropping Z (3D) if present."""
    nums = [float(v) for v in pos_text.split()]
    stride = 3 if len(nums) % 3 == 0 else 2
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums), stride)]


def _building_footprint(bldg: ET.Element):  # noqa: C901
# C901-Ausnahme: namensraum-agnostisches CityGML: mehrere Schreibweisen je Element
    """(Multi)Polygon from a building's GroundSurface exterior rings, or None."""
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.ops import unary_union

    polys = []
    for gs in (e for e in bldg.iter() if _local(e.tag) == "GroundSurface"):
        for poly in (e for e in gs.iter() if _local(e.tag) == "Polygon"):
            ext = next((e for e in poly.iter() if _local(e.tag) == "exterior"), None)
            if ext is None:
                continue
            pl = next((e for e in ext.iter() if _local(e.tag) == "posList"), None)
            if pl is None or not pl.text:
                continue
            ring = _ring_xy(pl.text)
            if len(ring) >= 4:
                try:
                    p = Polygon(ring)
                    if not p.is_valid:
                        p = p.buffer(0)
                    if not p.is_empty:
                        polys.append(p)
                except Exception:  # noqa: BLE001 - skip a malformed ring
                    continue
    if not polys:
        return None
    merged = unary_union(polys)
    if merged.geom_type == "Polygon":
        return MultiPolygon([merged])
    if merged.geom_type == "MultiPolygon":
        return merged
    return None


def _building_record(bldg: ET.Element) -> dict | None:
    """One building → {geometry, measured_height, street, housenumber, gml_id}."""
    geom = _building_footprint(bldg)
    if geom is None:
        return None
    heights = [float(e.text) for e in bldg.iter()
               if _local(e.tag) == "measuredHeight" and e.text and e.text.strip()]
    street = next((e.text.strip() for e in bldg.iter()
                   if _local(e.tag) == "ThoroughfareName" and e.text), None)
    num = next((e.text.strip() for e in bldg.iter()
                if _local(e.tag) == "ThoroughfareNumber" and e.text), None)
    gml_id = next((v for k, v in bldg.attrib.items() if k.endswith("}id") or k == "id"),
                  None)
    return {
        "geometry": geom,
        "measured_height": max(heights) if heights else None,
        "street": street,
        "housenumber": num,
        "gml_id": gml_id,
    }


def parse_citygml(path: str, epsg: int = 25832):
    """Stream a CityGML LoD2 tile into a GeoDataFrame (one row per building).

    Columns: ``measured_height`` (m), ``street``, ``housenumber``, ``gml_id``,
    ``geometry`` (footprint MultiPolygon in EPSG:``epsg``). Namespace-agnostic
    (matches local tag names), so it reads any state's AdV-standard LoD2.
    """
    import geopandas as gpd

    records = []
    for _event, elem in ET.iterparse(path, events=("end",)):
        if _local(elem.tag) != "Building":
            continue
        try:
            rec = _building_record(elem)
            if rec is not None:
                records.append(rec)
        finally:
            elem.clear()
    if not records:
        return gpd.GeoDataFrame(
            columns=["measured_height", "street", "housenumber", "gml_id", "geometry"],
            geometry="geometry", crs=f"EPSG:{epsg}",
        )
    return gpd.GeoDataFrame(records, geometry="geometry", crs=f"EPSG:{epsg}")


# ── fetch orchestration ──────────────────────────────────────────────────────

_UA = {"User-Agent": "Mozilla/5.0 (Chester GeoAI)"}


def _download(url: str, dest: str, mirrors: tuple[str, ...] = ()) -> bool:
    """Download ``url`` to ``dest`` (try mirror hosts on failure). True on success."""
    candidates = [url]
    for m in mirrors:
        # swap the host of a mirror-family URL (e.g. download1 → download2)
        for base in mirrors:
            if url.startswith(base):
                candidates.append(m + url[len(base):])
                break
    seen, urls = set(), []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            urls.append(u)
    for u in urls:
        try:
            with urlopen(Request(u, headers=_UA), timeout=120) as r:
                if r.status != 200:
                    continue
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            return True
        except Exception:  # noqa: BLE001 - try the next mirror
            continue
    return False


def _citygml_from(dest: str) -> str | None:
    """A parseable CityGML path for a downloaded tile — extracting a zip if needed.

    Some states (Brandenburg) ship each tile as a ``.zip`` around the ``.gml``;
    the plain-``.gml`` states return ``dest`` unchanged.
    """
    if not dest.lower().endswith(".zip"):
        return dest
    import zipfile

    out_dir = dest + "_x"
    os.makedirs(out_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(dest) as zf:
            member = next((n for n in zf.namelist()
                           if n.lower().endswith((".gml", ".xml"))), None)
            if member is None:
                return None
            target = os.path.join(out_dir, os.path.basename(member))
            if not (os.path.exists(target) and os.path.getsize(target) > 0):
                with zf.open(member) as fh, open(target, "wb") as f:
                    f.write(fh.read())
            return target
    except Exception:  # noqa: BLE001 - a corrupt zip = a missing tile
        return None


def detect_state(bbox_wgs84: list[float]) -> StateSource | None:
    """Which open state covers ``bbox`` — probe each state's centre tile (HEAD).

    Open states' tile grids don't overlap, so the first source that actually
    serves the bbox-centre tile is the right Bundesland — no boundary data needed.
    """
    from urllib.request import Request as _R
    from urllib.request import urlopen as _open

    cx = [(bbox_wgs84[0] + bbox_wgs84[2]) / 2] * 2
    cy = [(bbox_wgs84[1] + bbox_wgs84[3]) / 2] * 2
    centre = [cx[0], cy[0], cx[1], cy[1]]
    for src in open_states():
        tiles = src.resolver(centre) if src.resolver else []
        if not tiles:
            continue
        url = tiles[0][0]
        try:
            req = _R(url, headers=_UA, method="HEAD")
            with _open(req, timeout=20) as r:
                if r.status == 200:
                    return src
        except Exception:  # noqa: BLE001 - not this state
            continue
    return None


def _resolve_source(bbox_wgs84, state):
    """(StateSource, None) for a bbox/state, or (None, error_dict)."""
    if state:
        src = BUNDESLAENDER.get(state.upper())
        if src is None:
            return None, {"ok": False, "error": f"unknown Bundesland '{state}'",
                          "known": sorted(BUNDESLAENDER)}
        if src.status != "open" or src.resolver is None:
            return None, {"ok": False, "error": f"{src.name} LoD2 is open but not "
                          f"yet wired in Chester (access via {src.portal}); wired "
                          f"states: {[s.code for s in open_states()]}"}
        return src, None
    src = detect_state(bbox_wgs84)
    if src is None:
        return None, {"ok": False, "error": "bbox is not covered by a wired open "
                      "LoD2 source (currently " + ", ".join(f"{s.name} ({s.code})"
                      for s in open_states()) + "). For other Bundesländer see "
                      "lod2_sources().", "bbox": bbox_wgs84}
    return src, None


def download_citygml_tiles(bbox_wgs84: list[float], tile_cache_dir: str,
                           state: str | None = None) -> dict:
    """Download the CityGML LoD2 tiles covering ``bbox`` (zipped ones extracted).

    The download half of ``fetch_lod2`` without the GeoPackage parse — feeds the
    CityJSON pipeline (``chester/citymodel.py``). Returns the local ``.gml`` paths,
    the Bundesland, its EPSG and licence.
    """
    src, err = _resolve_source(bbox_wgs84, state)
    if err:
        return err
    tiles = src.resolver(bbox_wgs84)
    if not tiles:
        return {"ok": False, "error": "no tiles derived for the bbox"}
    os.makedirs(tile_cache_dir, exist_ok=True)
    gml_paths, missing = [], []
    for url, name in tiles:
        dest = os.path.join(tile_cache_dir, name)
        if not (os.path.exists(dest) and os.path.getsize(dest) > 0):
            if not _download(url, dest, src.mirrors):
                missing.append(name)
                continue
        data_path = _citygml_from(dest)
        if data_path is None:
            missing.append(f"{name} (no gml in zip)")
            continue
        gml_paths.append(data_path)
    if not gml_paths:
        return {"ok": False, "error": "no CityGML tiles could be downloaded",
                "state": src.code, "tiles_missing": missing}
    return {"ok": True, "state": src.code, "state_name": src.name, "epsg": src.epsg,
            "gml_paths": gml_paths, "tiles_missing": missing, "licence": src.licence}


def fetch_lod2(  # noqa: C901
# C901-Ausnahme: Absicherungen: Landeserkennung, Kachelkappe, fehlende Kacheln, Zip-Auspacken,
# Strassenfilter - jeder Zweig ein Fehlerfall
    bbox_wgs84: list[float],
    output_path: str,
    tile_cache_dir: str,
    state: str | None = None,
    street: str | None = None,
) -> dict:
    """Fetch open LoD2 buildings for ``bbox`` into ``output_path`` (a GeoPackage).

    Resolves the Bundesland (explicit ``state`` code or auto-detected), downloads
    the covering CityGML tiles (cached in ``tile_cache_dir``), parses footprint +
    ``measured_height`` + address, clips to the bbox, optionally filters to a
    ``street``, and writes a GeoPackage in the tiles' metric CRS (EPSG:25832/25833
    — heights and areas already in metres). Returns counts, height stats, CRS and
    the source licence.
    """
    import geopandas as gpd
    from shapely.geometry import box

    if state:
        src = BUNDESLAENDER.get(state.upper())
        if src is None:
            return {"ok": False, "error": f"unknown Bundesland '{state}'",
                    "known": sorted(BUNDESLAENDER)}
        if src.status != "open" or src.resolver is None:
            return {"ok": False, "error": f"{src.name} LoD2 is open but not yet wired "
                    f"in Chester (access via {src.portal}); wired states: "
                    f"{[s.code for s in open_states()]}"}
    else:
        src = detect_state(bbox_wgs84)
        if src is None:
            return {"ok": False, "error": "bbox is not covered by a wired open LoD2 "
                    "source (currently " + ", ".join(f"{s.name} ({s.code})"
                    for s in open_states()) + "). For other Bundesländer see "
                    "lod2_sources().",
                    "bbox": bbox_wgs84}

    # Der Zweig oben kehrt zurueck, wenn keine offene Quelle passt.
    assert src.resolver is not None
    tiles = src.resolver(bbox_wgs84)
    if not tiles:
        return {"ok": False, "error": "no tiles derived for the bbox"}

    os.makedirs(tile_cache_dir, exist_ok=True)
    frames, used, missing = [], [], []
    for url, name in tiles:
        dest = os.path.join(tile_cache_dir, name)
        if not (os.path.exists(dest) and os.path.getsize(dest) > 0):
            if not _download(url, dest, src.mirrors):
                missing.append(name)
                continue
        data_path = _citygml_from(dest)
        if data_path is None:
            missing.append(f"{name} (no gml in zip)")
            continue
        try:
            gdf = parse_citygml(data_path, epsg=src.epsg)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{name} (parse: {type(exc).__name__})")
            continue
        if not gdf.empty:
            frames.append(gdf)
            used.append(name)

    if not frames:
        return {"ok": False, "error": "no buildings parsed from the covering tiles",
                "state": src.code, "tiles_missing": missing}

    buildings = gpd.GeoDataFrame(
        __import__("pandas").concat(frames, ignore_index=True),
        geometry="geometry", crs=frames[0].crs,
    )

    # Clip to the area of interest (tiles are 1–2 km; the bbox is usually smaller).
    minx, miny, maxx, maxy = _bbox_in(bbox_wgs84, src.epsg)
    aoi = gpd.GeoSeries([box(minx, miny, maxx, maxy)], crs=buildings.crs)
    buildings = buildings[buildings.intersects(aoi.union_all())]

    if street:
        s = street.strip().lower()
        buildings = buildings[buildings["street"].fillna("").str.lower().str.startswith(s)]

    if buildings.empty:
        return {"ok": False, "error": "no buildings in the bbox"
                + (f" on street '{street}'" if street else ""),
                "state": src.code, "tiles_used": used}

    buildings.to_file(output_path, driver="GPKG", layer="buildings")

    h = buildings["measured_height"].dropna()
    return {
        "ok": True,
        "output": output_path,
        "state": src.code,
        "state_name": src.name,
        "buildings": int(len(buildings)),
        "with_height": int(len(h)),
        "height_stats_m": None if h.empty else {
            "min": round(float(h.min()), 2), "max": round(float(h.max()), 2),
            "mean": round(float(h.mean()), 2), "median": round(float(h.median()), 2),
        },
        "crs": f"EPSG:{src.epsg}",
        "tiles_used": used,
        "tiles_missing": missing,
        "licence": src.licence,
    }
