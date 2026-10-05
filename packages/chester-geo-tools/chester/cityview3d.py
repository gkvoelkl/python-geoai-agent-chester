"""The interactive 3D city view — one self-contained HTML file.

Split out of `citymodel.py` on 2026-10-05. `render_cityjson_html_3d` assembles the
glTF mesh (`cityexport`), the optional ground, relief and point cloud (`cityscene`) and
a three.js page into a single file that opens without a server. Kept apart because the
page template alone is seventy lines and the assembly is the module's whole job.
"""

from __future__ import annotations

import json
from pathlib import Path

from chester.cityexport import (
    _cdn_guard,
    _decompress_vertices,
    _epsg_from_dict,
    cityjson_to_glb_bytes,
)
from chester.cityscene import _fetch_relief_grid, _ground_texture_png, _pointcloud_points

# A glb larger than this is too heavy to inline into a dashboard iframe (a 7.8 MB
# model froze the browser); above it we skip the HTML and point at qgis_show_3d —
# mirroring render_map's inline-size guard.
_MAX_INLINE_3D_MB = 4.0


def render_cityjson_html_3d(cityjson_path: str | None, output_html: str,  # noqa: C901, PLR0915
# C901 exception: optional parts (basemap, relief, point cloud, size brake) - each branch
# is one option
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
        assert pts_col is not None  # set together with pts_xyz
        col_u8 = np.clip(pts_col * 255.0, 0, 255).astype("uint8")
        col_b64 = base64.b64encode(np.ascontiguousarray(col_u8).tobytes()).decode()

    # size guard: the actual inlined payload (glb + the two base64 strings)
    total_bytes = len(glb) + len(pos_b64) + len(col_b64)
    if total_bytes > _MAX_INLINE_3D_MB * 1_000_000:
        # `ok: False` for the same reason render_map's guards use it: the scene was
        # never written, so there is no path to quote and nothing to describe.
        return {
            "ok": False, "embedded": False, "buildings": n, "points": npts,
            "size_mb": round(total_bytes / 1e6, 1),
            "reason": f"the 3D scene is {round(total_bytes / 1e6, 1)} MB — too heavy to "
            "embed inline. NO file was written. " + (
                "**Still 3D and still in the browser: `render_buildings_3d(style=\"blocks\")`** "
                "renders the same buildings as extruded blocks (MapLibre, no roof "
                "shapes) and stays small. " if glb else "") +
            "Otherwise narrow the bbox, lower max_points, or "
            "view it in QGIS (qgis_show_3d / qgis_show_pointcloud). A flat map is not "
            "a substitute: if 3D was asked for, say what you delivered instead.",
            "recommend_tool": "qgis_show_pointcloud" if not glb else "qgis_show_3d",
        }

    # extent (recentred) for the OSM ground plate — from buildings if present, else points
    plane, basemap_uri, relief_json = {}, "", "null"
    if (basemap or relief) and epsg is not None:
        if V is not None:
            mn, mx = V.min(axis=0), V.max(axis=0)
        else:
            # Reachable only without buildings — and the guard above already returned
            # in that case unless a point cloud exists, so pts_xyz is set here.
            assert pts_xyz is not None
            mn = pts_xyz.min(axis=0) + center
            mx = pts_xyz.max(axis=0) + center
        from pyproj import Transformer

        tr = Transformer.from_crs(epsg, 4326, always_xy=True)
        w, s = tr.transform(mn[0], mn[1])
        e, nth = tr.transform(mx[0], mx[1])
        png = _ground_texture_png([w, s, e, nth])
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
                    assert center is not None  # gesetzt, sobald es Geometrie gibt
                    cz = float(center[2])
                    grid["z"] = [round(v - cz, 2) for v in grid["z"]]
                    relief_json = json.dumps(grid)

    glb_uri = ("data:model/gltf-binary;base64," + base64.b64encode(glb).decode("ascii")
               if glb else "")
    html = (_THREEJS_TEMPLATE
            .replace("__CDN_GUARD__", _cdn_guard("THREE", "unpkg.com"))
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
</head><body>__CDN_GUARD__<script>
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
