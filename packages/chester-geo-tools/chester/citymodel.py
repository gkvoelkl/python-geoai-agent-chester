"""CityGML → CityJSON writer — pure Python, no Java.

Step 1 of the CityJSON 3D pipeline. Chester's LoD2 sources ship **CityGML** (`.gml`,
via `fetch_lod2`), and there is **no Java-free off-the-shelf CityGML→CityJSON
converter** — citygml-tools / citygml4j are Java, FME is commercial, `cjio` reads
CityJSON only, and GDAL/QGIS has no CityJSON driver. So Chester writes CityJSON
itself from its own `ElementTree` parse: this module reads the CityGML LoD2 semantic
surfaces (GroundSurface / WallSurface / RoofSurface, **keeping Z**) and serialises
them as CityJSON 1.1 — the input for `cjio` and the QGIS CityJSON Loader plugin.

Mapping CityGML → CityJSON:
- each `bldg:Building` (any `BuildingPart` surfaces flattened in) → one CityObject
  `type: "Building"`;
- its semantic-surface polygons → one LoD2 **MultiSurface** geometry with per-surface
  `semantics` (Ground/Wall/Roof);
- shared vertices are deduplicated (rounded to mm) and quantised via a `transform`.

Pure standard library (`xml.etree`, `json`) — no new dependency, no Java.
"""

from __future__ import annotations

import contextlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

_CITYJSON_VERSION = "1.1"
# CityGML semantic-surface localname → CityJSON semantic surface type.
_SURFACE_TYPES = {
    "GroundSurface": "GroundSurface",
    "WallSurface": "WallSurface",
    "RoofSurface": "RoofSurface",
    "ClosureSurface": "ClosureSurface",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _epsg_from_srs(srs: str | None) -> int | None:
    """ETRS89/UTM srsName → EPSG (25832 for UTM32, 25833 for UTM33), else any code."""
    if not srs:
        return None
    s = srs.upper()
    if "UTM32" in s or "25832" in s:
        return 25832
    if "UTM33" in s or "25833" in s:
        return 25833
    m = re.search(r"(\d{4,5})", s)
    return int(m.group(1)) if m else None


def _ring_xyz(pos_text: str) -> list[tuple[float, float, float]]:
    """A gml:posList body → [(x, y, z), …], dropping a repeated closing vertex.

    CityJSON rings are implicitly closed, so the trailing duplicate CityGML emits is
    removed.
    """
    nums = [float(v) for v in pos_text.split()]
    pts = [(nums[i], nums[i + 1], nums[i + 2]) for i in range(0, len(nums) - 2, 3)]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _polygon_rings(poly: ET.Element) -> list[list[tuple[float, float, float]]]:
    """A gml:Polygon → [exterior_ring, interior_ring, …] as coordinate lists."""
    rings: list[list[tuple[float, float, float]]] = []
    for kind in ("exterior", "interior"):
        for boundary in (e for e in poly.iter() if _local(e.tag) == kind):
            pl = next((e for e in boundary.iter() if _local(e.tag) == "posList"), None)
            if pl is not None and pl.text and pl.text.strip():
                ring = _ring_xyz(pl.text)
                if len(ring) >= 3:
                    rings.append(ring)
    return rings


def _building_surfaces(bldg: ET.Element):
    """[(semantic_type, [rings]), …] for every semantic-surface polygon of a building."""
    out = []
    for surface in bldg.iter():
        stype = _SURFACE_TYPES.get(_local(surface.tag))
        if stype is None:
            continue
        for poly in (e for e in surface.iter() if _local(e.tag) == "Polygon"):
            rings = _polygon_rings(poly)
            if rings:
                out.append((stype, rings))
    return out


def _building_attributes(bldg: ET.Element) -> dict:
    """measuredHeight / roofType / function / address for a building (best-effort)."""
    attrs: dict = {}
    heights = [float(e.text) for e in bldg.iter()
               if _local(e.tag) == "measuredHeight" and e.text and e.text.strip()]
    if heights:
        attrs["measuredHeight"] = max(heights)
    for tag, key in (("roofType", "roofType"), ("function", "function")):
        v = next((e.text for e in bldg.iter()
                  if _local(e.tag) == tag and e.text and e.text.strip()), None)
        if v:
            attrs[key] = v.strip()
    street = next((e.text for e in bldg.iter()
                   if _local(e.tag) == "ThoroughfareName" and e.text), None)
    num = next((e.text for e in bldg.iter()
                if _local(e.tag) == "ThoroughfareNumber" and e.text), None)
    if street:
        attrs["address"] = (street.strip() + (f" {num.strip()}" if num else "")).strip()
    return attrs


def _building_id(bldg: ET.Element, fallback: str) -> str:
    return next((v for k, v in bldg.attrib.items() if k.endswith("}id") or k == "id"),
                fallback)


def _convert(gml_paths: list[str], epsg: int | None):  # noqa: C901
# C901-Ausnahme: CityGML kennt viele Geometrievarianten; jeder Zweig ist eine davon
    """Parse the CityGML tiles into one CityJSON dict (shared vertex pool)."""
    vlist: list[tuple[float, float, float]] = []     # unique float vertices
    vindex: dict[tuple[float, float, float], int] = {}  # rounded key → index

    def vid(pt: tuple[float, float, float]) -> int:
        key = (round(pt[0], 3), round(pt[1], 3), round(pt[2], 3))
        i = vindex.get(key)
        if i is None:
            i = len(vlist)
            vindex[key] = i
            vlist.append(pt)
        return i

    city_objects: dict = {}
    n = 0
    for path in gml_paths:
        root = ET.parse(path).getroot()
        if epsg is None:
            srs = next((e.attrib["srsName"] for e in root.iter()
                        if "srsName" in e.attrib), None)
            epsg = _epsg_from_srs(srs)
        for bldg in (e for e in root.iter() if _local(e.tag) == "Building"):
            surfaces = _building_surfaces(bldg)
            if not surfaces:
                continue
            boundaries: list = []
            sem_values: list = []
            sem_types: list[str] = []
            sem_seen: dict[str, int] = {}
            for stype, rings in surfaces:
                boundaries.append([[vid(p) for p in ring] for ring in rings])
                if stype not in sem_seen:
                    sem_seen[stype] = len(sem_types)
                    sem_types.append(stype)
                sem_values.append(sem_seen[stype])
            geom = {
                "type": "MultiSurface",
                "lod": "2",
                "boundaries": boundaries,
                "semantics": {"surfaces": [{"type": t} for t in sem_types],
                              "values": sem_values},
            }
            obj: dict = {"type": "Building", "geometry": [geom]}
            attrs = _building_attributes(bldg)
            if attrs:
                obj["attributes"] = attrs
            gid = str(_building_id(bldg, f"Building_{n}"))
            while gid in city_objects:            # keep ids unique across tiles
                gid = f"{gid}_{n}"
            city_objects[gid] = obj
            n += 1

    return _assemble(vlist, city_objects, epsg)


def _assemble(vlist, city_objects, epsg) -> dict:
    """Quantise the vertex pool via a transform and assemble the CityJSON dict."""
    if vlist:
        xs, ys, zs = zip(*vlist)
        minx, miny, minz = min(xs), min(ys), min(zs)
        maxx, maxy, maxz = max(xs), max(ys), max(zs)
    else:
        minx = miny = minz = maxx = maxy = maxz = 0.0
    scale = [0.001, 0.001, 0.001]  # mm precision
    verts = [[int(round((x - minx) / scale[0])),
              int(round((y - miny) / scale[1])),
              int(round((z - minz) / scale[2]))] for x, y, z in vlist]
    cj = {
        "type": "CityJSON",
        "version": _CITYJSON_VERSION,
        "transform": {"scale": scale, "translate": [minx, miny, minz]},
        "metadata": {"geographicalExtent": [minx, miny, minz, maxx, maxy, maxz]},
        "CityObjects": city_objects,
        "vertices": verts,
    }
    if epsg:
        cj["metadata"]["referenceSystem"] = (
            f"https://www.opengis.net/def/crs/EPSG/0/{epsg}")
    return cj


def citygml_to_cityjson(gml_path: str, epsg: int | None = None) -> dict:
    """One CityGML LoD2 tile → a CityJSON 1.1 dict (footprint/wall/roof + attributes)."""
    return _convert([str(gml_path)], epsg)


def cityjson_from_solids(buildings, epsg: int | None) -> dict:
    """Assemble a CityJSON 1.1 dict from prepared 3D building **solids**.

    The non-CityGML counterpart to ``_convert``: some LoD2 sources ship a closed 3D
    solid per building (a triangulated multipatch), not the split ground/wall/roof
    semantic surfaces — e.g. swissBUILDINGS3D's ``Building_solid``. Each building
    becomes one LoD2 ``MultiSurface`` (every solid face = one surface, exterior ring
    only; no per-surface semantics, since the source has no ground/wall/roof split).
    Vertices are deduplicated (mm) and quantised via ``_assemble`` — identical to the
    CityGML path, so the output feeds the same renderers / GeoPackage / QGIS tools.

    ``buildings`` = iterable of ``(obj_id, attributes, faces)`` where ``faces`` is a
    list of exterior rings, each a list of ``(x, y, z)`` (implicitly closed — pass
    the ring **without** the repeated closing vertex).
    """
    vlist: list[tuple[float, float, float]] = []
    vindex: dict[tuple[float, float, float], int] = {}

    def vid(pt: tuple[float, float, float]) -> int:
        key = (round(pt[0], 3), round(pt[1], 3), round(pt[2], 3))
        i = vindex.get(key)
        if i is None:
            i = len(vlist)
            vindex[key] = i
            vlist.append(pt)
        return i

    city_objects: dict = {}
    for oid, attrs, faces in buildings:
        boundaries = [[[vid(p) for p in ring]] for ring in faces if len(ring) >= 3]
        if not boundaries:
            continue
        obj: dict = {
            "type": "Building",
            "geometry": [{"type": "MultiSurface", "lod": "2", "boundaries": boundaries}],
        }
        if attrs:
            obj["attributes"] = attrs
        gid = str(oid)
        while gid in city_objects:
            gid = f"{gid}_{len(city_objects)}"
        city_objects[gid] = obj

    return _assemble(vlist, city_objects, epsg)


def write_cityjson(gml_paths, output_path: str, epsg: int | None = None) -> dict:
    """Convert one or more CityGML tiles into a single CityJSON file.

    ``gml_paths`` is a path or a list of paths (tiles covering an area merge into one
    model, vertices deduplicated). ``epsg`` is auto-detected from the CityGML
    ``srsName`` if not given. Returns counts + the CRS.
    """
    if isinstance(gml_paths, (str, Path)):
        gml_paths = [gml_paths]
    cj = _convert([str(p) for p in gml_paths], epsg)
    Path(output_path).write_text(json.dumps(cj), encoding="utf-8")
    ref = cj["metadata"].get("referenceSystem")
    return {
        "ok": True,
        "output": str(output_path),
        "buildings": len(cj["CityObjects"]),
        "vertices": len(cj["vertices"]),
        "crs": (f"EPSG:{ref.rsplit('/', 1)[-1]}" if ref else None),
        "cityjson_version": _CITYJSON_VERSION,
    }


# ── cjio reader + bbox subset (downstream — reads any CityJSON, ours or a portal's) ─


def _import_cityjson():
    """Import `cjio.cityjson` — and undo what its import does to the whole process.

    `cjio/cityjson.py` patches the **standard library** at import time
    (`json.encoder.c_make_encoder = None`, `json.encoder.float = FloatEncoder`) to
    shrink its own output. It never restores it, so from that moment every
    `json.dumps` in the process writes fixed six-decimal floats: provenance
    sidecars, `last_map.json`, tool returns, eval records. `12.1` becomes
    `12.100000` — harmless — but `1.2e-09` becomes `0.000000`, which is a silently
    wrong number in a result the model then reports.

    Found 2026-09-14: one test's map differed from the same map rendered outside
    pytest, because another test had loaded `cjio` first. The reach is the whole
    session, not one call.

    Restoring right after the import costs `cjio` nothing we depend on — its
    encoder was a file-size optimisation for CityJSON it writes, and Chester writes
    CityJSON through `json.dump` itself (`write_cityjson`).
    """
    missing = object()
    # `float` is not normally an attribute of `json.encoder` at all — cjio *adds*
    # one that shadows the builtin. Restoring therefore means removing it again,
    # not writing a previous value back.
    before = {
        name: getattr(json.encoder, name, missing)
        for name in ("c_make_encoder", "float")
    }
    try:
        from cjio import cityjson
    finally:
        for name, value in before.items():
            if value is missing:
                with contextlib.suppress(AttributeError):
                    delattr(json.encoder, name)
            else:
                setattr(json.encoder, name, value)
    return cityjson


def load_cityjson(path: str):
    """Load a CityJSON file into a `cjio` ``CityJSON`` object (the downstream model).

    `cjio` is the gateway to CityJSON operations Chester doesn't hand-roll — bbox
    subsetting (below) and, later, glTF / b3dm / OBJ export for display. Pure Python.
    """
    cityjson = _import_cityjson()

    with open(path) as fp:
        return cityjson.CityJSON(file=fp)


def _cityjson_epsg(cj) -> int | None:
    ref = (cj.j.get("metadata") or {}).get("referenceSystem")
    if not ref:
        return None
    m = re.search(r"(\d{4,5})\D*$", ref) or re.search(r"(\d{4,5})", ref)
    return int(m.group(1)) if m else None


def subset_bbox(input_path: str, output_path: str, bbox_wgs84: list[float],
                epsg: int | None = None) -> dict:
    """Subset a CityJSON file to the buildings within a WGS84 ``bbox`` via `cjio`.

    ``bbox_wgs84`` = [west, south, east, north]. The model's CRS (from its
    ``referenceSystem``, or ``epsg``) is used to reproject the bbox before the
    `cjio` ``get_subset_bbox`` — tiles are 1–2 km, so this clips to the exact area
    of interest. Writes a standalone CityJSON and returns the count.
    """
    cj = load_cityjson(input_path)
    if epsg is None:
        epsg = _cityjson_epsg(cj)
    if epsg:
        from pyproj import Transformer

        tr = Transformer.from_crs(4326, epsg, always_xy=True)
        _w, _s, _e, _n = bbox_wgs84
        minx, miny, maxx, maxy = tr.transform_bounds(_w, _s, _e, _n)
    else:
        minx, miny, maxx, maxy = bbox_wgs84

    sub = cj.get_subset_bbox([minx, miny, maxx, maxy])
    n = len(sub.j.get("CityObjects", {}))
    if n == 0:
        return {"ok": False, "error": "no buildings in the bbox",
                "crs": (f"EPSG:{epsg}" if epsg else None)}
    Path(output_path).write_text(json.dumps(sub.j), encoding="utf-8")
    return {
        "ok": True,
        "output": str(output_path),
        "buildings": n,
        "vertices": len(sub.j.get("vertices", [])),
        "crs": (f"EPSG:{epsg}" if epsg else None),
    }


# ── display: CityJSON → self-contained 3D HTML (MapLibre fill-extrusion, 2.5D) ──
#
# The default web display (Tier A): MapLibre GL
# extrudes the 2D footprints itself by height — no triangulation, no glTF, no heavy
# dependency (the real-roof three.js/Tier-B path needs a licence-safe triangulator,
# a later step). Fed from the CityJSON: per building, the GroundSurface footprint +
# `measuredHeight` (or the geometry's Z-range), reprojected to WGS84 and drawn as an
# extruded block on an OSM basemap, all inlined into one standalone HTML file.


# ── display: CityJSON → glTF → self-contained three.js HTML (Tier B, real roofs) ─
#
# The fidelity display (Tier B): the real LoD2 shells
# (roof shapes, not flat blocks). cjio's glb export needs the non-commercially
# licensed `triangle` package, so Chester triangulates itself — Newell-normal plane
# projection + `mapbox_earcut` (MIT) per surface, then `trimesh` (MIT) packs the
# triangles (coloured by surface type) into a glb, embedded in a three.js viewer.


# ── Point-cloud overlay (LiDAR → decimated three.js Points) ──────────────────────────
#
# The web point-cloud path shares the three.js viewer with the LoD2 buildings. A LAS/
# LAZ/COPC is decimated to ~max_points and exported to XYZ+Classification via PDAL
# (`qgis_process`, since geopandas can't read point clouds), reprojected to the model
# CRS and recentred to the same origin as the buildings, then embedded as `THREE.Points`.


