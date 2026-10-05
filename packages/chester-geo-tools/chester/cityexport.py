"""CityJSON out: as a 3D GeoPackage, as glTF bytes, as a 2D web map.

Split out of `citymodel.py` on 2026-10-05 (1150 lines). `citymodel` turns CityGML into
CityJSON and loads it; this writes CityJSON into the forms other tools read —
`cityjson_to_gpkg_z` (footprints with Z for QGIS), `cityjson_to_glb_bytes` (the
triangulated, semantically coloured mesh the 3D view embeds) and `render_cityjson_html`
(a MapLibre map of extruded footprints). The vertex helpers live here because every
export needs them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


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


def _outline_from_solid(obj, verts):
    """Footprint of a building whose surfaces carry **no** semantics: the 2D union.

    A solid source (swissBUILDINGS3D's triangulated multipatch, via
    ``cityjson_from_solids``) has no ground/wall/roof split, so there is no
    GroundSurface to look up. Projected to 2D, the walls of a closed solid collapse
    to zero-area slivers and ground and roof project onto the same outline — their
    union is the footprint. One building arrives as ~100 triangles, so this unions
    per building, not per face.
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    faces = []
    for g in obj.get("geometry", []):
        for surface in g.get("boundaries", []):
            if not surface or len(surface[0]) < 3:
                continue
            p = Polygon([(verts[i][0], verts[i][1]) for i in surface[0]])
            if not p.is_valid:
                p = p.buffer(0)  # self-touching triangles from the source mesh
            if not p.is_empty and p.area > 0:  # drops the vertical walls
                faces.append(p)
    if not faces:
        return []
    merged = unary_union(faces)
    parts = merged.geoms if hasattr(merged, "geoms") else [merged]
    return [list(p.exterior.coords)[:-1] for p in parts if not p.is_empty and p.area > 0]


def _footprint_and_height(obj, verts):
    """(list of ground exterior rings [(x,y)…], height) for a building CityObject.

    Footprint = its GroundSurface exterior rings (via semantics); height = the
    ``measuredHeight`` attribute, else the geometry's Z-range.

    Without semantics the footprint is derived from the solid itself — see
    ``_outline_from_solid``. Until 2026-09-01 this returned nothing in that case, and
    ``render_cityjson_html`` answered *"no building footprints to render"* for every
    Swiss model, while the three.js path rendered the same file fine (it treats a
    missing surface type as "unknown" and triangulates anyway).
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
    if not rings2d:
        rings2d = _outline_from_solid(obj, verts)
    attrs = obj.get("attributes") or {}
    # Both spellings: CityGML writes `measuredHeight`, `cityjson_from_solids` carries
    # the Swiss source's `measured_height` through unchanged.
    height = attrs.get("measuredHeight", attrs.get("measured_height"))
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
    html = html.replace("__CDN_GUARD__", _cdn_guard("maplibregl", "unpkg.com"))
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


def cityjson_to_glb_bytes(cj_dict, center=None) -> tuple:  # noqa: C901
# C901 exception: CityJSON geometry types plus triangulation special cases
    """CityJSON dict → (glb bytes, building count). Recentred to ``center`` (the model
    centroid if not given) — pass the same center to align a basemap plane."""
    import numpy as np
    import trimesh

    verts = _decompress_vertices(cj_dict)
    if not verts:
        return b"", 0
    if center is None:
        center = np.asarray(verts).mean(axis=0)
    V: list = []
    F: list = []
    C: list = []
    n_buildings = 0
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


def _cdn_guard(global_name: str, host: str) -> str:
    """An in-page notice for the case where the viewer library never arrived.

    Both 3D pages embed their *data* but still fetch their *library* from a CDN when
    opened. Measured 2026-08-19 against a dead host: the page came up completely
    empty, and the only trace was a ``ReferenceError`` in the browser console —
    invisible to the person looking at the page and to the agent that produced it,
    which makes a network problem indistinguishable from a broken render.

    This runs **before** the main script, so the message is already in the DOM by the
    time that script throws on the missing global; the throw then aborts nothing that
    still matters. Deliberately no retry and no fallback CDN — the point is to say
    what happened, not to paper over it.
    """
    return (
        "<script>\n"
        f'if(typeof {global_name}==="undefined"){{document.body.innerHTML='
        "'<div style=\"font:14px/1.6 system-ui,-apple-system,sans-serif;color:#243b53;"
        "background:#fff;padding:22px 26px;max-width:44em;margin:12vh auto;"
        "border:1px solid #d8dde3;border-radius:10px\">'"
        "+'<b>The 3D view could not load.</b><br><br>This page fetches its viewer "
        f"library from <code>{host}</code> when you open it, and that request did not "
        "succeed. The building model itself is embedded in this file and is intact — "
        "only the viewer is missing.<br><br>Check the network connection or proxy, or "
        "open the layer in QGIS Desktop instead.</div>';}\n"
        "</script>"
    )


_MAPLIBRE_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel="stylesheet">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>html,body,#map{height:100%;margin:0}</style></head>
<body><div id="map"></div>__CDN_GUARD__<script>
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
