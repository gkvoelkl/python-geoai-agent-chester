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

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import Request, urlopen

_UA = {"User-Agent": "Mozilla/5.0 (Chester GeoAI)"}

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


def _convert(gml_paths: list[str], epsg: int | None):
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
            boundaries, sem_values, sem_types, sem_seen = [], [], [], {}
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


def load_cityjson(path: str):
    """Load a CityJSON file into a `cjio` ``CityJSON`` object (the downstream model).

    `cjio` is the gateway to CityJSON operations Chester doesn't hand-roll — bbox
    subsetting (below) and, later, glTF / b3dm / OBJ export for display. Pure Python.
    """
    from cjio import cityjson

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
        minx, miny, maxx, maxy = tr.transform_bounds(*bbox_wgs84)
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


def _epsg_from_dict(cj_dict) -> int | None:
    ref = (cj_dict.get("metadata") or {}).get("referenceSystem")
    if not ref:
        return None
    m = re.search(r"(\d{4,5})\D*$", ref) or re.search(r"(\d{4,5})", ref)
    return int(m.group(1)) if m else None


def _decompress_vertices(cj_dict):
    t = cj_dict.get("transform")
    verts = cj_dict.get("vertices", [])
    if t:
        (sx, sy, sz), (tx, ty, tz) = t["scale"], t["translate"]
        return [(v[0] * sx + tx, v[1] * sy + ty, v[2] * sz + tz) for v in verts]
    return [(v[0], v[1], v[2]) for v in verts]


def _footprint_and_height(obj, verts):
    """(list of ground exterior rings [(x,y)…], height) for a building CityObject.

    Footprint = its GroundSurface exterior rings (via semantics); height = the
    ``measuredHeight`` attribute, else the geometry's Z-range.
    """
    rings2d, zs = [], []
    for g in obj.get("geometry", []):
        boundaries = g.get("boundaries", [])
        sem = g.get("semantics") or {}
        surfaces, values = sem.get("surfaces", []), sem.get("values", [])
        for i, surface in enumerate(boundaries):
            for ring in surface:
                zs.extend(verts[idx][2] for idx in ring)
            is_ground = (i < len(values) and values[i] is not None
                         and values[i] < len(surfaces)
                         and surfaces[values[i]].get("type") == "GroundSurface")
            if is_ground and surface:
                rings2d.append([(verts[idx][0], verts[idx][1]) for idx in surface[0]])
    height = (obj.get("attributes") or {}).get("measuredHeight")
    if height is None and zs:
        height = round(max(zs) - min(zs), 2)
    return rings2d, (height or 0.0)


def render_cityjson_html(cityjson_path: str, output_html: str, title: str = "") -> dict:
    """Render a CityJSON to a standalone 3D HTML (MapLibre extruded blocks).

    Extracts each building's footprint + height from the CityJSON, reprojects to
    WGS84, and inlines it into a self-contained HTML that MapLibre GL extrudes into
    a 2.5D block model on an OpenStreetMap basemap (coloured by height). Returns the
    path, building count and map centre.
    """
    from pyproj import Transformer

    cj = json.loads(Path(cityjson_path).read_text(encoding="utf-8"))
    epsg = _epsg_from_dict(cj)
    verts = _decompress_vertices(cj)
    tr = Transformer.from_crs(epsg or 4326, 4326, always_xy=True)

    features, lons, lats = [], [], []
    for oid, obj in cj.get("CityObjects", {}).items():
        if obj.get("type") not in (None, "Building", "BuildingPart"):
            continue
        rings2d, height = _footprint_and_height(obj, verts)
        polys = []
        for ring in rings2d:
            coords = []
            for x, y in ring:
                lon, lat = tr.transform(x, y)
                coords.append([lon, lat])
                lons.append(lon)
                lats.append(lat)
            if len(coords) >= 3:
                coords.append(coords[0])  # GeoJSON rings are closed
                polys.append([coords])
        if polys:
            features.append({
                "type": "Feature",
                "properties": {"height": round(float(height), 2), "id": str(oid)},
                "geometry": {"type": "MultiPolygon", "coordinates": polys},
            })

    if not features:
        return {"ok": False, "error": "no building footprints to render"}

    center = [sum(lons) / len(lons), sum(lats) / len(lats)]
    geojson = {"type": "FeatureCollection", "features": features}
    html = _MAPLIBRE_TEMPLATE
    html = html.replace("__TITLE__", title or "Chester — 3D buildings")
    html = html.replace("__CENTER__", json.dumps(center))
    html = html.replace("__GEOJSON__", json.dumps(geojson))
    Path(output_html).write_text(html, encoding="utf-8")
    return {"ok": True, "output": str(output_html), "buildings": len(features),
            "center": center}


def cityjson_to_gpkg_z(cityjson_path: str, output_gpkg: str) -> dict:
    """CityJSON → a **MultiPolygonZ** GeoPackage QGIS reads natively in its 3D view.

    Each building's semantic-surface polygons (ground/wall/roof) become 3D faces of
    one MultiPolygon Z feature (attributes: `id`, `measured_height`), in the model
    CRS. QGIS' 3D Map View renders the real LoD2 shells — **zero-plugin**, no
    triangulation. This is the layer the live QGIS bridge (`qgis_show`) then loads.
    """
    import geopandas as gpd
    from shapely.geometry import MultiPolygon, Polygon

    cj = json.loads(Path(cityjson_path).read_text(encoding="utf-8"))
    epsg = _epsg_from_dict(cj)
    verts = _decompress_vertices(cj)

    records = []
    for oid, obj in cj.get("CityObjects", {}).items():
        if obj.get("type") not in (None, "Building", "BuildingPart"):
            continue
        faces = []
        for g in obj.get("geometry", []):
            for surface in g.get("boundaries", []):
                if not surface or len(surface[0]) < 3:
                    continue
                ext = [verts[i] for i in surface[0]]
                holes = [[verts[i] for i in ring] for ring in surface[1:]
                         if len(ring) >= 3]
                try:
                    faces.append(Polygon(ext, holes))
                except Exception:  # noqa: BLE001 - skip a malformed face
                    continue
        if not faces:
            continue
        rec = {"id": str(oid), "geometry": MultiPolygon(faces)}
        h = (obj.get("attributes") or {}).get("measuredHeight")
        rec["measured_height"] = float(h) if h is not None else None
        records.append(rec)

    if not records:
        return {"ok": False, "error": "no building surfaces to write"}
    gdf = gpd.GeoDataFrame(records, geometry="geometry",
                           crs=f"EPSG:{epsg}" if epsg else None)
    gdf.to_file(output_gpkg, driver="GPKG", layer="buildings")
    return {"ok": True, "output": str(output_gpkg), "buildings": len(records),
            "crs": f"EPSG:{epsg}" if epsg else None, "geometry_z": True}


# ── display: CityJSON → glTF → self-contained three.js HTML (Tier B, real roofs) ─
#
# The fidelity display (Tier B): the real LoD2 shells
# (roof shapes, not flat blocks). cjio's glb export needs the non-commercially
# licensed `triangle` package, so Chester triangulates itself — Newell-normal plane
# projection + `mapbox_earcut` (MIT) per surface, then `trimesh` (MIT) packs the
# triangles (coloured by surface type) into a glb, embedded in a three.js viewer.

_SURFACE_RGB = {
    "RoofSurface": [200, 96, 66],
    "WallSurface": [205, 205, 210],
    "GroundSurface": [120, 120, 122],
    None: [180, 180, 186],
}


def _newell_normal(pts):
    import numpy as np

    n = np.zeros(3)
    m = len(pts)
    for i in range(m):
        a, b = pts[i], pts[(i + 1) % m]
        n[0] += (a[1] - b[1]) * (a[2] + b[2])
        n[1] += (a[2] - b[2]) * (a[0] + b[0])
        n[2] += (a[0] - b[0]) * (a[1] + b[1])
    return n


def _triangulate_rings(rings3d):
    """[(exterior, holes…)] 3D rings → (points 3D, [(i,j,k) triangles]) via earcut.

    The polygon is projected onto its dominant plane (drop the axis of the largest
    normal component), earcut-triangulated in 2D, and the indices reused for the 3D
    points. Winding is irrelevant — the viewer renders both sides.
    """
    import mapbox_earcut as earcut
    import numpy as np

    normal = _newell_normal(rings3d[0])
    if not np.any(normal):
        return None
    keep = [k for k in range(3) if k != int(np.argmax(np.abs(normal)))]
    pts3d, flat2d, ring_ends = [], [], []
    for ring in rings3d:
        for p in ring:
            pts3d.append(p)
            flat2d.append([p[keep[0]], p[keep[1]]])
        ring_ends.append(len(flat2d))
    idx = earcut.triangulate_float64(np.asarray(flat2d, dtype=np.float64),
                                     np.asarray(ring_ends))
    if len(idx) < 3:
        return None
    tris = [(int(idx[i]), int(idx[i + 1]), int(idx[i + 2]))
            for i in range(0, len(idx) - 2, 3)]
    return pts3d, tris


def cityjson_to_glb_bytes(cj_dict, center=None) -> tuple:
    """CityJSON dict → (glb bytes, building count). Recentred to ``center`` (the model
    centroid if not given) — pass the same center to align a basemap plane."""
    import numpy as np
    import trimesh

    verts = _decompress_vertices(cj_dict)
    if not verts:
        return b"", 0
    if center is None:
        center = np.asarray(verts).mean(axis=0)
    V, F, C, n_buildings = [], [], [], 0
    for obj in cj_dict.get("CityObjects", {}).values():
        if obj.get("type") not in (None, "Building", "BuildingPart"):
            continue
        contributed = False
        for g in obj.get("geometry", []):
            boundaries = g.get("boundaries", [])
            sem = g.get("semantics") or {}
            surfaces, values = sem.get("surfaces", []), sem.get("values", [])
            for i, surface in enumerate(boundaries):
                if not surface or len(surface[0]) < 3:
                    continue
                stype = (surfaces[values[i]].get("type")
                         if i < len(values) and values[i] is not None
                         and values[i] < len(surfaces) else None)
                rings3d = [[np.asarray(verts[idx]) - center for idx in ring]
                           for ring in surface]
                tri = _triangulate_rings(rings3d)
                if tri is None:
                    continue
                pts, tris = tri
                off = len(V)
                V.extend(pts)
                C.extend([_SURFACE_RGB.get(stype, _SURFACE_RGB[None])] * len(pts))
                F.extend([[off + a, off + b, off + c] for a, b, c in tris])
                contributed = True
        if contributed:
            n_buildings += 1

    if not F:
        return b"", 0
    colors = np.hstack([np.asarray(C, dtype=np.uint8),
                        np.full((len(C), 1), 255, dtype=np.uint8)])  # RGBA
    mesh = trimesh.Trimesh(vertices=np.asarray(V), faces=np.asarray(F),
                           vertex_colors=colors, process=False)
    return mesh.export(file_type="glb"), n_buildings


# A glb larger than this is too heavy to inline into a dashboard iframe (a 7.8 MB
# model froze the browser); above it we skip the HTML and point at qgis_show_3d —
# mirroring render_map's inline-size guard.
_MAX_INLINE_3D_MB = 4.0


def _deg2tile(lat: float, lon: float, z: int) -> tuple:
    """WGS84 lat/lon → fractional slippy-map tile coords at zoom ``z``."""
    import math

    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


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


# ── Point-cloud overlay (LiDAR → decimated three.js Points) ──────────────────────────
#
# The web point-cloud path shares the three.js viewer with the LoD2 buildings. A LAS/
# LAZ/COPC is decimated to ~max_points and exported to XYZ+Classification via PDAL
# (`qgis_process`, since geopandas can't read point clouds), reprojected to the model
# CRS and recentred to the same origin as the buildings, then embedded as `THREE.Points`.

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
    import re
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


def render_cityjson_html_3d(cityjson_path: str | None, output_html: str,
                            title: str = "", basemap: bool = True,
                            relief: bool = False, pointcloud: str | None = None,
                            pointcloud_epsg: int | None = None,
                            max_points: int = 150_000) -> dict:
    """Render LoD2 buildings and/or a LiDAR **point cloud** to a standalone three.js HTML.

    Buildings (a CityJSON) are triangulated (earcut) → a glb coloured by surface type;
    an optional ``pointcloud`` (LAS/LAZ/COPC) is decimated to ``max_points`` and overlaid
    as ``THREE.Points`` coloured by LAS classification, **reprojected to the buildings'
    CRS and recentred to the same origin** so the two align. Either may be omitted:
    buildings-only (the classic viewer), points-only (a web point-cloud view), or both.
    With ``basemap`` an OSM ground plate is laid under the data (optionally draped over a
    ``relief`` DGM1 mesh). A scene over ``_MAX_INLINE_3D_MB`` is **not** written (too heavy
    to inline) — narrow the bbox / lower ``max_points`` or use QGIS. Returns path + counts.
    """
    import base64

    import numpy as np

    # -- buildings (optional) --
    glb, n, center, epsg, V = b"", 0, None, None, None
    if cityjson_path and Path(cityjson_path).exists():
        cj = json.loads(Path(cityjson_path).read_text(encoding="utf-8"))
        verts = _decompress_vertices(cj)
        if verts:
            V = np.asarray(verts)
            center = V.mean(axis=0)
            epsg = _epsg_from_dict(cj)
            glb, n = cityjson_to_glb_bytes(cj, center=center)

    # -- point cloud (optional) — reproject to the buildings' CRS, recentre to `center` --
    pts_xyz, pts_col = None, None
    if pointcloud:
        pc = _pointcloud_points(pointcloud, target_epsg=epsg, src_epsg=pointcloud_epsg,
                                max_points=max_points)
        if pc is None:
            if not glb:
                return {"ok": False, "error": "could not read the point cloud"}
        else:
            pxyz = pc["xyz"]
            if center is None:  # points-only → centre on the points, adopt their CRS
                center = pxyz.mean(axis=0)
                epsg = pc["epsg"]
            pts_xyz = (pxyz - center).astype("float32")
            pts_col = pc["colors"].astype("float32")

    if not glb and pts_xyz is None:
        return {"ok": False, "error": "nothing to render (no buildings, no point cloud)"}

    # Embed points as base64 binary (position float32, colour uint8) — far more compact
    # than a JSON text array (which bloats ~5x and would freeze the dashboard iframe).
    npts, pos_b64, col_b64 = 0, "", ""
    if pts_xyz is not None:
        npts = int(len(pts_xyz))
        pos_b64 = base64.b64encode(np.ascontiguousarray(pts_xyz, "<f4").tobytes()).decode()
        col_u8 = np.clip(pts_col * 255.0, 0, 255).astype("uint8")
        col_b64 = base64.b64encode(np.ascontiguousarray(col_u8).tobytes()).decode()

    # size guard: the actual inlined payload (glb + the two base64 strings)
    total_bytes = len(glb) + len(pos_b64) + len(col_b64)
    if total_bytes > _MAX_INLINE_3D_MB * 1_000_000:
        return {
            "ok": True, "embedded": False, "buildings": n, "points": npts,
            "size_mb": round(total_bytes / 1e6, 1),
            "reason": f"the 3D scene is {round(total_bytes / 1e6, 1)} MB — too heavy to "
            "embed inline. Narrow the bbox, lower max_points, or view it in QGIS "
            "(qgis_show_3d / qgis_show_pointcloud).",
            "recommend_tool": "qgis_show_pointcloud" if not glb else "qgis_show_3d",
        }

    # extent (recentred) for the OSM ground plate — from buildings if present, else points
    plane, basemap_uri, relief_json = {}, "", "null"
    if (basemap or relief) and epsg is not None:
        if V is not None:
            mn, mx = V.min(axis=0), V.max(axis=0)
        else:
            mn = (pts_xyz.min(axis=0) + center)
            mx = (pts_xyz.max(axis=0) + center)
        from pyproj import Transformer

        tr = Transformer.from_crs(epsg, 4326, always_xy=True)
        w, s = tr.transform(mn[0], mn[1])
        e, nth = tr.transform(mx[0], mx[1])
        png = _osm_basemap_png([w, s, e, nth])
        if png:
            basemap_uri = "data:image/png;base64," + base64.b64encode(png).decode()
            rmn, rmx = mn - center, mx - center
            plane = {"w": float(mx[0] - mn[0]), "h": float(mx[1] - mn[1]),
                     "cx": float((rmn[0] + rmx[0]) / 2),
                     "cy": float((rmn[1] + rmx[1]) / 2),
                     "z": float(rmn[2] - 0.5)}
            if relief:
                grid = _fetch_relief_grid([w, s, e, nth], float(mn[0]), float(mn[1]),
                                          float(mx[0]), float(mx[1]))
                if grid and grid["z"]:
                    cz = float(center[2])
                    grid["z"] = [round(v - cz, 2) for v in grid["z"]]
                    relief_json = json.dumps(grid)

    glb_uri = ("data:model/gltf-binary;base64," + base64.b64encode(glb).decode("ascii")
               if glb else "")
    html = (_THREEJS_TEMPLATE
            .replace("__TITLE__", title or "Chester — 3D view")
            .replace("__BASEMAP__", basemap_uri)
            .replace("__PLANE__", json.dumps(plane))
            .replace("__RELIEF__", relief_json)
            .replace("__NPTS__", str(npts))
            .replace("__PTS_B64__", pos_b64)
            .replace("__PCOL_B64__", col_b64)
            .replace("__GLB__", glb_uri))
    Path(output_html).write_text(html, encoding="utf-8")
    return {"ok": True, "embedded": True, "output": str(output_html), "buildings": n,
            "points": npts, "size_kb": round(total_bytes / 1024, 1),
            "basemap": bool(basemap_uri), "relief": relief_json != "null"}


# Classic (non-module) three.js — global `THREE`, UMD loaders. Deliberately NOT the
# ES-module + importmap build: importmaps and `type="module"` are unreliable inside a
# sandboxed dashboard iframe, whereas classic <script> tags run there and standalone.
_THREEJS_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>html,body{height:100%;margin:0;background:#dfe6ee;overflow:hidden}</style>
<script src="https://unpkg.com/three@0.137.0/build/three.min.js"></script>
<script src="https://unpkg.com/three@0.137.0/examples/js/loaders/GLTFLoader.js"></script>
<script src="https://unpkg.com/three@0.137.0/examples/js/controls/OrbitControls.js"></script>
</head><body><script>
var scene=new THREE.Scene(); scene.background=new THREE.Color(0xdfe6ee);
var camera=new THREE.PerspectiveCamera(55,innerWidth/innerHeight,0.1,1e6);
camera.up.set(0,0,1);
var renderer=new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth,innerHeight); document.body.appendChild(renderer.domElement);
var controls=new THREE.OrbitControls(camera,renderer.domElement); controls.enableDamping=true;
scene.add(new THREE.HemisphereLight(0xffffff,0x556677,1.0));
var sun=new THREE.DirectionalLight(0xffffff,1.4); sun.position.set(1,-1,2); scene.add(sun);
var BM="__BASEMAP__", PL=__PLANE__, RE=__RELIEF__;
if(BM && PL.w){
  var tex=new THREE.TextureLoader().load(BM);
  var geo, mat, zpos;
  if(RE && RE.z){                       // DGM1 relief: displaced, shaded grid
    geo=new THREE.PlaneGeometry(PL.w,PL.h,RE.cols-1,RE.rows-1);
    var p=geo.attributes.position;
    for(var i=0;i<RE.z.length;i++){ p.setZ(i,RE.z[i]); }
    geo.computeVertexNormals();
    mat=new THREE.MeshStandardMaterial({map:tex,side:THREE.DoubleSide,roughness:1.0,metalness:0.0});
    zpos=0;                             // vertices already carry recentred elevation
  } else {                              // flat ground plate
    geo=new THREE.PlaneGeometry(PL.w,PL.h);
    mat=new THREE.MeshBasicMaterial({map:tex,side:THREE.DoubleSide});
    zpos=PL.z;
  }
  var ground=new THREE.Mesh(geo,mat); ground.position.set(PL.cx,PL.cy,zpos);
  scene.add(ground);
}
var GLB="__GLB__", NP=__NPTS__;
function frame(c,r){ r=r||50; controls.target.copy(c);
  camera.position.set(c.x+r*1.2,c.y-r*1.4,c.z+r*1.1);
  camera.far=r*20; camera.updateProjectionMatrix(); }
function b64bytes(s){var b=atob(s),u=new Uint8Array(b.length);
  for(var i=0;i<b.length;i++)u[i]=b.charCodeAt(i);return u;}
// LiDAR point cloud — base64 position (float32) + colour (uint8), recentred
if(NP){
  var pos=new Float32Array(b64bytes("__PTS_B64__").buffer);
  var col=b64bytes("__PCOL_B64__");
  var pg=new THREE.BufferGeometry();
  pg.setAttribute("position",new THREE.BufferAttribute(pos,3));
  pg.setAttribute("color",new THREE.BufferAttribute(col,3,true)); // normalized 0..1
  pg.computeBoundingSphere();
  var pts=new THREE.Points(pg,new THREE.PointsMaterial(
    {size:0.7,vertexColors:true,sizeAttenuation:true}));
  scene.add(pts);
  if(!GLB){ var bs=pg.boundingSphere; frame(bs.center,bs.radius*1.3); }
}
// LoD2 buildings (glb)
if(GLB){
  new THREE.GLTFLoader().load(GLB,function(gltf){
    gltf.scene.traverse(function(o){if(o.isMesh){o.material.side=THREE.DoubleSide;
      o.material.metalness=0.0;o.material.roughness=0.85;}});
    scene.add(gltf.scene);
    var box=new THREE.Box3().setFromObject(gltf.scene);
    var c=box.getCenter(new THREE.Vector3()), s=box.getSize(new THREE.Vector3());
    frame(c, Math.max(s.x,s.y,s.z));
  });
}
addEventListener("resize",function(){camera.aspect=innerWidth/innerHeight;
  camera.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight);});
(function loop(){requestAnimationFrame(loop);controls.update();renderer.render(scene,camera);})();
</script></body></html>"""


_MAPLIBRE_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>html,body,#map{height:100%;margin:0}</style></head>
<body><div id="map"></div><script>
const style={version:8,sources:{osm:{type:"raster",
  tiles:["https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"],tileSize:256,
  attribution:"© OpenStreetMap contributors"}},
  layers:[{id:"osm",type:"raster",source:"osm"}]};
const map=new maplibregl.Map({container:"map",style,center:__CENTER__,zoom:15.5,
  pitch:55,bearing:-17,maxPitch:75});
map.addControl(new maplibregl.NavigationControl({visualizePitch:true}));
map.on("load",()=>{
  map.addSource("buildings",{type:"geojson",data:__GEOJSON__});
  map.addLayer({id:"buildings3d",type:"fill-extrusion",source:"buildings",paint:{
    "fill-extrusion-height":["get","height"],
    "fill-extrusion-base":0,"fill-extrusion-opacity":0.92,
    "fill-extrusion-color":["interpolate",["linear"],["get","height"],
      0,"#f7fbff",10,"#9ecae1",25,"#3182bd",50,"#08306b"]}});
});
</script></body></html>"""
