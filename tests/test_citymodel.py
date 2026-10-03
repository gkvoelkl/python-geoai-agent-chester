"""Tests for the CityGML→CityJSON writer (chester/citymodel.py).

The mapping (semantic surfaces → CityJSON MultiSurface + semantics, vertex dedup,
transform, attributes) is covered offline with a synthetic CityGML building; a
real Bayern tile is one opt-in network test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from _util import requires_qgis

from chester import citymodel

# One 10×10 m building: GroundSurface (z=100), 2 WallSurfaces (vertical), RoofSurface
# (flat, z=110), measuredHeight 10, on Teststraße 7. Namespaces differ from the real
# tiles on purpose — the parser matches local tag names.
_CITYGML = """<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0"
    xmlns:bldg="http://www.opengis.net/citygml/building/2.0"
    xmlns:gml="http://www.opengis.net/gml"
    xmlns:xAL="urn:oasis:names:tc:ciq:xsdschema:xAL:2.0">
  <gml:boundedBy><gml:Envelope srsName="urn:adv:crs:ETRS89_UTM32*DE_DHHN2016_NH"
      srsDimension="3"/></gml:boundedBy>
  <core:cityObjectMember>
    <bldg:Building gml:id="B1">
      <bldg:measuredHeight uom="urn:adv:uom:m">10.0</bldg:measuredHeight>
      <bldg:roofType>1000</bldg:roofType>
      <bldg:address><core:Address><core:xalAddress><xAL:AddressDetails>
        <xAL:Thoroughfare><xAL:ThoroughfareNumber>7</xAL:ThoroughfareNumber>
        <xAL:ThoroughfareName>Teststraße</xAL:ThoroughfareName></xAL:Thoroughfare>
      </xAL:AddressDetails></core:xalAddress></core:Address></bldg:address>
      <bldg:boundedBy><bldg:GroundSurface><bldg:lod2MultiSurface><gml:MultiSurface>
        <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing>
        <gml:posList>0 0 100 10 0 100 10 10 100 0 10 100 0 0 100</gml:posList>
        </gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
      </gml:MultiSurface></bldg:lod2MultiSurface></bldg:GroundSurface></bldg:boundedBy>
      <bldg:boundedBy><bldg:RoofSurface><bldg:lod2MultiSurface><gml:MultiSurface>
        <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing>
        <gml:posList>0 0 110 10 0 110 10 10 110 0 10 110 0 0 110</gml:posList>
        </gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
      </gml:MultiSurface></bldg:lod2MultiSurface></bldg:RoofSurface></bldg:boundedBy>
      <bldg:boundedBy><bldg:WallSurface><bldg:lod2MultiSurface><gml:MultiSurface>
        <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing>
        <gml:posList>0 0 100 10 0 100 10 0 110 0 0 110 0 0 100</gml:posList>
        </gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
      </gml:MultiSurface></bldg:lod2MultiSurface></bldg:WallSurface></bldg:boundedBy>
      <bldg:boundedBy><bldg:WallSurface><bldg:lod2MultiSurface><gml:MultiSurface>
        <gml:surfaceMember><gml:Polygon><gml:exterior><gml:LinearRing>
        <gml:posList>10 0 100 10 10 100 10 10 110 10 0 110 10 0 100</gml:posList>
        </gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
      </gml:MultiSurface></bldg:lod2MultiSurface></bldg:WallSurface></bldg:boundedBy>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""


def _write(tmp_path):
    p = tmp_path / "b.gml"
    p.write_text(_CITYGML, encoding="utf-8")
    return str(p)


def test_writes_valid_cityjson_skeleton(tmp_path):
    cj = citymodel.citygml_to_cityjson(_write(tmp_path))
    assert cj["type"] == "CityJSON"
    assert cj["version"] == "1.1"
    assert "transform" in cj and cj["vertices"]
    # quantised vertices are integers
    assert all(isinstance(c, int) for v in cj["vertices"] for c in v)
    # CRS detected from the ETRS89/UTM32 srsName
    assert cj["metadata"]["referenceSystem"].endswith("/25832")


def test_building_becomes_one_multisurface_with_semantics(tmp_path):
    cj = citymodel.citygml_to_cityjson(_write(tmp_path))
    assert set(cj["CityObjects"]) == {"B1"}
    obj = cj["CityObjects"]["B1"]
    assert obj["type"] == "Building"
    geom = obj["geometry"][0]
    assert geom["type"] == "MultiSurface" and geom["lod"] == "2"
    # 1 ground + 1 roof + 2 walls = 4 surfaces
    assert len(geom["boundaries"]) == 4
    assert len(geom["semantics"]["values"]) == 4
    types = {s["type"] for s in geom["semantics"]["surfaces"]}
    assert types == {"GroundSurface", "RoofSurface", "WallSurface"}
    # the ground surface's semantic value points at a GroundSurface entry
    surfaces = geom["semantics"]["surfaces"]
    assert any(surfaces[v]["type"] == "RoofSurface" for v in geom["semantics"]["values"])


def test_attributes_and_ring_not_closed(tmp_path):
    cj = citymodel.citygml_to_cityjson(_write(tmp_path))
    attrs = cj["CityObjects"]["B1"]["attributes"]
    assert attrs["measuredHeight"] == 10.0
    assert attrs["roofType"] == "1000"
    assert attrs["address"] == "Teststraße 7"
    # each ring keeps 4 corners (the repeated closing vertex is dropped)
    for surface in cj["CityObjects"]["B1"]["geometry"][0]["boundaries"]:
        assert len(surface[0]) == 4


def test_shared_vertices_are_deduplicated(tmp_path):
    # ground + walls + roof share corners; 8 box corners → 8 unique vertices.
    cj = citymodel.citygml_to_cityjson(_write(tmp_path))
    assert len(cj["vertices"]) == 8


def test_write_cityjson_file_roundtrips(tmp_path):
    out = tmp_path / "b.city.json"
    r = citymodel.write_cityjson(_write(tmp_path), str(out))
    assert r["ok"] and r["buildings"] == 1 and r["crs"] == "EPSG:25832"
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded["type"] == "CityJSON"


def _two_building_gml(tmp_path):
    """A CityGML with two ground-only buildings ~4 km apart, in EPSG:25832."""
    def bldg(bid, e, n):
        z = 100
        ring = (f"{e} {n} {z} {e+10} {n} {z} {e+10} {n+10} {z} "
                f"{e} {n+10} {z} {e} {n} {z}")
        return (f'<core:cityObjectMember><bldg:Building gml:id="{bid}">'
                '<bldg:measuredHeight>10</bldg:measuredHeight>'
                '<bldg:boundedBy><bldg:GroundSurface><bldg:lod2MultiSurface>'
                '<gml:MultiSurface><gml:surfaceMember><gml:Polygon><gml:exterior>'
                f'<gml:LinearRing><gml:posList>{ring}</gml:posList></gml:LinearRing>'
                '</gml:exterior></gml:Polygon></gml:surfaceMember></gml:MultiSurface>'
                '</bldg:lod2MultiSurface></bldg:GroundSurface></bldg:boundedBy>'
                '</bldg:Building></core:cityObjectMember>')
    gml = (
        '<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0" '
        'xmlns:bldg="http://www.opengis.net/citygml/building/2.0" '
        'xmlns:gml="http://www.opengis.net/gml">'
        '<gml:boundedBy><gml:Envelope srsName="ETRS89_UTM32"/></gml:boundedBy>'
        + bldg("B1", 690000, 5776000) + bldg("B2", 693000, 5779000)
        + "</core:CityModel>")
    p = tmp_path / "two.gml"
    p.write_text(gml, encoding="utf-8")
    src = tmp_path / "two.city.json"
    citymodel.write_cityjson(str(p), str(src), epsg=25832)
    return str(src)


def _wgs84_bbox_around(e, n, pad=50):
    from pyproj import Transformer

    tr = Transformer.from_crs(25832, 4326, always_xy=True)
    w, s = tr.transform(e - pad, n - pad)
    ee, nn = tr.transform(e + 10 + pad, n + 10 + pad)
    return [w, s, ee, nn]


def test_load_cityjson_returns_cjio_model(tmp_path):
    cj = citymodel.load_cityjson(_two_building_gml(tmp_path))
    assert len(cj.j["CityObjects"]) == 2


def test_subset_bbox_keeps_only_buildings_in_the_bbox(tmp_path):
    src = _two_building_gml(tmp_path)
    out = tmp_path / "sub.city.json"
    r = citymodel.subset_bbox(src, str(out), _wgs84_bbox_around(690000, 5776000),
                              epsg=25832)
    assert r["ok"] and r["buildings"] == 1
    assert set(json.loads(out.read_text())["CityObjects"]) == {"B1"}


def test_subset_bbox_autodetects_epsg_from_reference_system(tmp_path):
    # epsg=None → read it from the CityJSON metadata our writer stamped.
    src = _two_building_gml(tmp_path)
    r = citymodel.subset_bbox(src, str(tmp_path / "s.city.json"),
                              _wgs84_bbox_around(693000, 5779000))
    assert r["ok"] and r["buildings"] == 1 and r["crs"] == "EPSG:25832"


def test_subset_bbox_empty_selection_reports(tmp_path):
    src = _two_building_gml(tmp_path)
    r = citymodel.subset_bbox(src, str(tmp_path / "e.city.json"),
                              [0.0, 0.0, 0.001, 0.001], epsg=25832)
    assert r["ok"] is False and "no buildings" in r["error"]


def test_render_cityjson_html_is_self_contained_extrusion(tmp_path):
    src = _two_building_gml(tmp_path)
    out = tmp_path / "buildings.html"
    r = citymodel.render_cityjson_html(src, str(out), title="Test")
    assert r["ok"] and r["buildings"] == 2
    html = out.read_text(encoding="utf-8")
    # self-contained MapLibre 3D viewer
    assert "maplibre-gl" in html and "fill-extrusion" in html
    assert '"fill-extrusion-height":["get","height"]' in html
    # the footprints are inlined as GeoJSON with a height property
    assert '"type": "FeatureCollection"' in html
    assert '"height"' in html
    # centre reprojected into WGS84 near the synthetic UTM32 location (Magdeburg-ish)
    assert 11.0 < r["center"][0] < 12.5 and 51.5 < r["center"][1] < 52.5


def test_render_cityjson_html_uses_measured_height(tmp_path):
    src = _two_building_gml(tmp_path)
    out = tmp_path / "b.html"
    citymodel.render_cityjson_html(src, str(out))
    # measuredHeight was 10 → the extruded height in the inlined GeoJSON
    assert '"height": 10.0' in out.read_text(encoding="utf-8")


def test_cityjson_to_gpkg_z_writes_multipolygonz(tmp_path):
    import geopandas as gpd

    src = tmp_path / "b.city.json"
    citymodel.write_cityjson(_write(tmp_path), str(src), epsg=25832)
    out = tmp_path / "b.gpkg"
    r = citymodel.cityjson_to_gpkg_z(str(src), str(out))
    assert r["ok"] and r["buildings"] == 1 and r["geometry_z"] and r["crs"] == "EPSG:25832"
    g = gpd.read_file(out)
    geom = g.geometry.iloc[0]
    assert geom.geom_type == "MultiPolygon" and geom.has_z
    assert len(geom.geoms) == 4  # ground + roof + 2 walls → 4 3D faces
    assert g["measured_height"].iloc[0] == 10.0


def test_triangulate_rings_square_gives_two_triangles():
    import numpy as np

    square = [[np.array([0.0, 0, 0]), np.array([10.0, 0, 0]),
              np.array([10.0, 10, 0]), np.array([0.0, 10, 0])]]
    pts, tris = citymodel._triangulate_rings(square)
    assert len(pts) == 4 and len(tris) == 2


def test_render_html_3d_embeds_a_valid_glb(tmp_path):
    import base64
    import io

    import trimesh

    src = tmp_path / "b.city.json"
    citymodel.write_cityjson(_write(tmp_path), str(src), epsg=25832)
    out = tmp_path / "b3d.html"
    r = citymodel.render_cityjson_html_3d(str(src), str(out), basemap=False)
    assert r["ok"] and r["embedded"] and r["buildings"] == 1 and r["size_kb"] > 0
    html = out.read_text(encoding="utf-8")
    # self-contained three.js viewer (classic scripts, no importmap) with glb inlined
    assert "three.min.js" in html and "GLTFLoader" in html
    assert "importmap" not in html and 'type="module"' not in html
    assert "model/gltf-binary;base64," in html
    # the embedded glb is a valid glTF that reloads with triangles
    b64 = html.split("base64,")[1].split('"')[0]
    mesh = trimesh.load(io.BytesIO(base64.b64decode(b64)), file_type="glb")
    tri = (sum(len(g.faces) for g in mesh.geometry.values())
           if hasattr(mesh, "geometry") else len(mesh.faces))
    assert tri >= 4  # the box's ground+roof+walls triangulate to several faces


def test_render_html_3d_guards_oversized_model(tmp_path, monkeypatch):
    # A model over the inline-size cap must NOT write a giant HTML — it returns
    # embedded=False and points at qgis_show_3d (mirrors render_map's guard).
    monkeypatch.setattr(citymodel, "_MAX_INLINE_3D_MB", 1e-9)  # force the guard
    src = tmp_path / "b.city.json"
    citymodel.write_cityjson(_write(tmp_path), str(src), epsg=25832)
    out = tmp_path / "big.html"
    r = citymodel.render_cityjson_html_3d(str(src), str(out))
    # `ok: false`, because nothing was written. It said `ok: true` until 2026-08-25,
    # and a run that got the same shape from render_map answered with a map link and
    # an excuse for why the file might not open. A success with no artefact is not one.
    assert r["ok"] is False and r["embedded"] is False
    assert r["recommend_tool"] == "qgis_show_3d" and "size_mb" in r
    assert not out.exists()  # no oversized HTML written
    assert "NO file was written" in r["reason"]


def test_looks_like_cityjson_detects_by_content_and_extension(tmp_path):
    from chester.capabilities.qgis_live import _looks_like_cityjson

    cj = tmp_path / "x.json"
    cj.write_text('{"type":"CityJSON","version":"1.1","CityObjects":{}}')
    gj = tmp_path / "y.json"
    gj.write_text('{"type":"FeatureCollection","features":[]}')
    assert _looks_like_cityjson(str(cj)) is True          # by content
    assert _looks_like_cityjson(str(gj)) is False          # a GeoJSON is not
    assert _looks_like_cityjson(str(tmp_path / "z.city.json")) is True  # by extension


def _citymodel_tools(tmp_path):
    from agent_build import _capability_tools
    from chester.capabilities import GeoCityModelCapability

    return _capability_tools(GeoCityModelCapability(workspace=str(tmp_path)))


def test_capability_render_buildings_3d_roofs_and_blocks(tmp_path):
    src = tmp_path / "m.city.json"
    citymodel.write_cityjson(_write(tmp_path), str(src), epsg=25832)
    tools = _citymodel_tools(tmp_path)
    r = tools["render_buildings_3d"](cityjson_path=str(src), output_path="r.html",
                                     basemap=False)
    assert r["ok"] and "three.min.js" in Path(r["output"]).read_text(encoding="utf-8")
    r2 = tools["render_buildings_3d"](cityjson_path=str(src), output_path="b.html",
                                      style="blocks")
    assert r2["ok"] and "maplibre-gl" in Path(r2["output"]).read_text(encoding="utf-8")


def test_render_buildings_3d_declares_the_hosts_the_page_still_needs(tmp_path):
    """Both viewers pull their JS from a CDN when *opened* — that must be stated.

    Measured 2026-08-19 by pointing a generated page at a dead host: it comes up as
    an empty page, and the only trace is a `ReferenceError` in the browser console —
    nothing on the page, and nothing an agent can ever observe. `ok: true` here means
    a file was written, not that anyone will see a building.

    The list is checked against the page's own `src=` URLs, so a template that
    changes its CDN cannot silently make this note wrong.
    """
    src = tmp_path / "m.city.json"
    citymodel.write_cityjson(_write(tmp_path), str(src), epsg=25832)
    tools = _citymodel_tools(tmp_path)

    roofs = tools["render_buildings_3d"](cityjson_path=str(src), output_path="r.html",
                                         basemap=False)
    blocks = tools["render_buildings_3d"](cityjson_path=str(src), output_path="b.html",
                                          style="blocks")
    for result, expected_hosts in ((roofs, {"unpkg.com"}),
                                   (blocks, {"unpkg.com", "a.tile.openstreetmap.org"})):
        html = Path(result["output"]).read_text(encoding="utf-8")
        declared = {h.split()[0] for h in result["needs_online"]}
        assert declared == expected_hosts, result["needs_online"]
        # Every declared host really is fetched by the page …
        assert all(host in html for host in declared)
        # … and no external host is fetched that we failed to declare.
        found = {m.split("//")[1].split("/")[0]
                 for m in re.findall(r'https://[^"\'{\s]+', html)}
        assert found <= declared, f"nicht deklarierte Hosts: {found - declared}"
        assert "EMPTY page" in result["offline_note"]


def test_a_page_whose_cdn_is_unreachable_says_so_on_the_page(tmp_path):
    """An empty window is indistinguishable from a broken render — so don't ship one.

    Verified in a browser on 2026-08-19 by pointing both generated pages at a dead
    host: each showed the notice instead of the blank field they used to show, and
    with the CDN reachable neither guard fired (the Dom still rendered).

    The **ordering** is the load-bearing part and is what this test pins: the guard
    has to run before the main script, because that script throws on the missing
    global the moment it touches it. Behind it, the message would never be written.
    """
    src = tmp_path / "m.city.json"
    citymodel.write_cityjson(_write(tmp_path), str(src), epsg=25832)
    tools = _citymodel_tools(tmp_path)

    for style, global_name, first_use in (("roofs", "THREE", "new THREE.Scene"),
                                          ("blocks", "maplibregl", "new maplibregl.Map")):
        out = tools["render_buildings_3d"](cityjson_path=str(src), basemap=False,
                                           output_path=f"{style}.html", style=style)
        html = Path(out["output"]).read_text(encoding="utf-8")
        guard = html.index(f'typeof {global_name}==="undefined"')
        assert guard < html.index(first_use), f"{style}: Guard steht hinter dem Skript"
        assert "could not load" in html and "unpkg.com" in html
        # It must not fire on a healthy page: the check is on the global, nothing else.
        assert html.count(f'typeof {global_name}==="undefined"') == 1


def test_a_point_cloud_forces_the_three_js_viewer_even_with_style_blocks(tmp_path):
    """`blocks` cannot draw points, so the note must describe the renderer that ran."""
    src = tmp_path / "m.city.json"
    citymodel.write_cityjson(_write(tmp_path), str(src), epsg=25832)
    missing = tmp_path / "nope.laz"
    r = _citymodel_tools(tmp_path)["render_buildings_3d"](
        cityjson_path=str(src), output_path="x.html", style="blocks",
        pointcloud=str(missing))
    # The point cloud is missing, so this fails before rendering — the guard we care
    # about is that `blocks` + a point cloud never reaches the MapLibre branch.
    assert r["ok"] is False and "point cloud" in r["error"]


def test_capability_cityjson_to_geopackage(tmp_path):
    src = tmp_path / "m.city.json"
    citymodel.write_cityjson(_write(tmp_path), str(src), epsg=25832)
    r = _citymodel_tools(tmp_path)["cityjson_to_geopackage"](
        cityjson_path=str(src), output_path="m.gpkg")
    assert r["ok"] and r["geometry_z"] and Path(r["output"]).exists()


def test_capability_render_missing_cityjson_errors(tmp_path):
    r = _citymodel_tools(tmp_path)["render_buildings_3d"](
        cityjson_path=str(tmp_path / "nope.city.json"), output_path="x.html")
    assert r["ok"] is False and "no such CityJSON" in r["error"]


def test_render_buildings_3d_requires_an_input(tmp_path):
    r = _citymodel_tools(tmp_path)["render_buildings_3d"](
        cityjson_path=None, output_path="x.html")
    assert r["ok"] is False and "point cloud" in r["error"]


@requires_qgis
@pytest.mark.network
def test_render_pointcloud_only_web_3d(tmp_path):
    """Points-only web 3D: a COPC → decimated THREE.Points embedded (base64), self-
    contained and under the inline-size guard."""
    import urllib.request

    laz = tmp_path / "cloud.copc.laz"
    urllib.request.urlretrieve(
        "https://s3.amazonaws.com/hobu-lidar/autzen-classified.copc.laz", str(laz))
    out = tmp_path / "pc.html"
    r = citymodel.render_cityjson_html_3d(None, str(out), pointcloud=str(laz),
                                          max_points=80_000)
    assert r["ok"] and r["embedded"] and r["buildings"] == 0 and r["points"] > 0
    h = out.read_text(encoding="utf-8")
    assert "THREE.Points" in h and "__PTS_B64__" not in h  # placeholder filled
    assert out.stat().st_size < 4_000_000  # base64 stays under the inline guard


@pytest.mark.network
def test_capability_fetch_cityjson_end_to_end(tmp_path):
    # Maximilianstraße bbox → LoD2 CityGML → CityJSON, clipped, with provenance.
    tools = _citymodel_tools(tmp_path)
    r = tools["fetch_cityjson"](bbox=[12.0955, 49.0135, 12.1015, 49.0195],
                                output_path="max.city.json")
    assert r["ok"], r
    assert r["state"] == "BY" and r["buildings"] > 100 and r["crs"] == "EPSG:25832"
    import json

    cj = json.loads(Path(r["output"]).read_text(encoding="utf-8"))
    assert cj["type"] == "CityJSON" and cj["CityObjects"]


@pytest.mark.network
def test_render_html_3d_adds_osm_ground_plate(tmp_path):
    # basemap=True lays an OSM raster ground plate under the buildings (real coords).
    src = _two_building_gml(tmp_path)  # real UTM32 coords → OSM tiles exist
    out = tmp_path / "plate.html"
    r = citymodel.render_cityjson_html_3d(src, str(out), basemap=True)
    assert r["ok"] and r["basemap"] is True
    html = out.read_text(encoding="utf-8")
    assert "data:image/png;base64," in html and "PlaneGeometry" in html


@pytest.mark.network
def test_render_html_3d_dgm1_relief(tmp_path):
    # relief=True drapes the OSM plate over a DGM1 terrain mesh — real Bayern coords
    # (Regensburg) so fetch_dgm1 is covered.
    e, n, z = 727000, 5433800, 335
    ring = (f"{e} {n} {z} {e+10} {n} {z} {e+10} {n+10} {z} "
            f"{e} {n+10} {z} {e} {n} {z}")
    gml = (
        '<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0" '
        'xmlns:bldg="http://www.opengis.net/citygml/building/2.0" '
        'xmlns:gml="http://www.opengis.net/gml">'
        '<gml:boundedBy><gml:Envelope srsName="ETRS89_UTM32"/></gml:boundedBy>'
        '<core:cityObjectMember><bldg:Building gml:id="B1">'
        '<bldg:measuredHeight>10</bldg:measuredHeight><bldg:boundedBy><bldg:GroundSurface>'
        '<bldg:lod2MultiSurface><gml:MultiSurface><gml:surfaceMember><gml:Polygon>'
        f'<gml:exterior><gml:LinearRing><gml:posList>{ring}</gml:posList></gml:LinearRing>'
        '</gml:exterior></gml:Polygon></gml:surfaceMember></gml:MultiSurface>'
        '</bldg:lod2MultiSurface></bldg:GroundSurface></bldg:boundedBy></bldg:Building>'
        '</core:cityObjectMember></core:CityModel>')
    (tmp_path / "rgb.gml").write_text(gml, encoding="utf-8")
    src = tmp_path / "rgb.city.json"
    citymodel.write_cityjson(str(tmp_path / "rgb.gml"), str(src), epsg=25832)
    out = tmp_path / "relief.html"
    r = citymodel.render_cityjson_html_3d(str(src), str(out), relief=True)
    assert r["ok"] and r["relief"] is True
    html = out.read_text(encoding="utf-8")
    assert '"cols":' in html and '"z":' in html and "PlaneGeometry" in html


@pytest.mark.network
def test_converts_a_real_bayern_tile(tmp_path):
    import urllib.request

    gml = tmp_path / "726_5432.gml"
    url = "https://download1.bayernwolke.de/a/lod2/citygml/726_5432.gml"
    data = urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
        timeout=90).read()
    gml.write_bytes(data)

    out = tmp_path / "tile.city.json"
    r = citymodel.write_cityjson(str(gml), str(out), epsg=25832)
    assert r["ok"] and r["buildings"] > 500 and r["vertices"] > 1000
    cj = json.loads(out.read_text(encoding="utf-8"))
    # a real building carries semantic roof/wall/ground surfaces
    some = next(iter(cj["CityObjects"].values()))
    assert some["geometry"][0]["type"] == "MultiSurface"
    assert some["geometry"][0]["semantics"]["surfaces"]


def _solid_box(x0=2600000.0, y0=1200000.0, w=10.0, h=5.0):
    """A closed box as a solid's faces — the shape `cityjson_from_solids` takes.

    Rings are exterior only and **not** closed (no repeated last vertex), matching
    swissBUILDINGS3D's multipatch after `swisstopo` reads it.
    """
    g = [(x0, y0, 0.0), (x0 + w, y0, 0.0), (x0 + w, y0 + w, 0.0), (x0, y0 + w, 0.0)]
    r = [(x, y, h) for x, y, _ in g]
    walls = [[g[i], g[(i + 1) % 4], r[(i + 1) % 4], r[i]] for i in range(4)]
    return [g, r, *walls]


def test_blocks_render_works_without_semantics(tmp_path):
    # The Swiss path: a solid, no ground/wall/roof split. Before 2026-09-01 this
    # answered "no building footprints to render" for every swissBUILDINGS3D model,
    # while the three.js path rendered the same file — found by the benchmark case
    # swiss-buildings-3d-bern-kramgasse.
    cj = citymodel.cityjson_from_solids(
        [("b1", {"measured_height": 5.0}, _solid_box())], epsg=2056)
    assert not cj["CityObjects"]["b1"]["geometry"][0].get("semantics")  # the premise

    src = tmp_path / "solid.city.json"
    src.write_text(json.dumps(cj), encoding="utf-8")
    out = tmp_path / "blocks.html"
    r = citymodel.render_cityjson_html(str(src), str(out))

    assert r["ok"] and r["buildings"] == 1
    assert '"height": 5.0' in out.read_text(encoding="utf-8")  # measured_height honoured


def test_solid_footprint_is_the_outline_not_a_single_triangle(tmp_path):
    # The union has to run per building: a solid arrives as many faces, and picking
    # one would extrude a sliver. The box is 10 x 10, so the footprint is 100 m².
    from shapely.geometry import Polygon

    cj = citymodel.cityjson_from_solids([("b1", {}, _solid_box())], epsg=2056)
    verts = citymodel._decompress_vertices(cj)
    rings, height = citymodel._footprint_and_height(cj["CityObjects"]["b1"], verts)

    assert len(rings) == 1
    assert Polygon(rings[0]).area == pytest.approx(100.0, abs=0.5)
    assert height == pytest.approx(5.0, abs=0.01)  # from the Z-range, no attribute


def test_semantic_ground_surface_still_wins_over_the_solid_fallback(tmp_path):
    # The German LoD2 path must be untouched: with semantics present the fallback
    # never runs, so a GroundSurface stays the footprint.
    cj = citymodel.citygml_to_cityjson(_write(tmp_path))
    verts = citymodel._decompress_vertices(cj)
    obj = next(iter(cj["CityObjects"].values()))
    assert obj["geometry"][0]["semantics"]["surfaces"]  # the premise
    rings, _ = citymodel._footprint_and_height(obj, verts)
    assert len(rings) == 1


def test_loading_cjio_leaves_the_stdlib_json_encoder_alone():
    """cjio darf nicht die Zahlenausgabe des ganzen Prozesses umstellen.

    `cjio/cityjson.py` setzt beim Import `json.encoder.c_make_encoder = None` und
    legt ein `json.encoder.float` an, das Gleitkommazahlen fest mit sechs
    Nachkommastellen schreibt — und nimmt es nie zurück. Danach schreibt **jedes**
    `json.dumps` im Prozess so: Provenance-Sidecars, `last_map.json`,
    Werkzeugrückgaben, Eval-Protokolle. `12.1` wird zu `12.100000` (harmlos),
    `1.2e-09` aber zu `0.000000` — eine still falsche Zahl in einem Ergebnis, das
    das Modell anschliessend berichtet.

    Gefunden am 14.09.2026: Dieselbe Karte sah in pytest anders aus als ausserhalb,
    weil ein früherer Test `cjio` geladen hatte.
    """
    import json

    from chester.citymodel import _import_cityjson

    cityjson = _import_cityjson()
    assert hasattr(cityjson, "CityJSON")          # cjio ist wirklich geladen
    assert json.dumps(1.2e-9) == "1.2e-09"        # und die Stdlib unverändert
    assert json.dumps(12.1) == "12.1"


def _tools(ws):
    from chester.citymodeltools import build_tools

    return {f.__name__: f for f in build_tools(str(ws))}


def test_a_geopackage_is_refused_as_cityjson_with_the_condition_named(tmp_path):
    """A 3D view needs CityJSON — and the refusal has to say so, not leak a decode error.

    Measured twice on 2026-09-27 (`city3d-regensburg-dom-height`, then
    `city3d-html-maximilianstrasse`): handed a GeoPackage of footprints,
    `render_buildings_3d` passed the existence check and died inside the JSON parser
    with `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa0`. Both runs then
    delivered a flat map and reported `ok: true`, and in one of them the ressort's
    correct diagnosis ("requires a CityJSON file") ended up as prose in `report` while
    the structured `needs` field stayed empty.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    cache = tmp_path / "geocache"
    cache.mkdir(parents=True)
    gpkg = cache / "buildings.gpkg"
    gpd.GeoDataFrame({"h": [12.0]}, geometry=[Point(12.1, 49.0)],
                     crs="EPSG:4326").to_file(gpkg, driver="GPKG")

    res = _tools(tmp_path)["render_buildings_3d"](
        cityjson_path="buildings.gpkg", output_path="view.html")
    assert res["ok"] is False
    assert "UnicodeDecodeError" not in res["error"], "the symptom is not the answer"
    assert "GeoPackage, not CityJSON" in res["error"]
    assert "fetch_cityjson" in res["error"], "name where CityJSON comes from"
    assert "no converter back" in res["error"], "say that the way back does not exist"
    assert "render_map" in res["error"], "and name the honest 2D fallback"


def test_the_converter_refuses_the_same_way(tmp_path):
    """`cityjson_to_geopackage` sits behind the same door and had the same trap."""
    import geopandas as gpd
    from shapely.geometry import Point

    cache = tmp_path / "geocache"
    cache.mkdir(parents=True)
    gpd.GeoDataFrame({"h": [1.0]}, geometry=[Point(12.1, 49.0)],
                     crs="EPSG:4326").to_file(cache / "b.gpkg", driver="GPKG")
    res = _tools(tmp_path)["cityjson_to_geopackage"](
        cityjson_path="b.gpkg", output_path="out.gpkg")
    assert res["ok"] is False and "not CityJSON" in res["error"]


def test_a_real_cityjson_is_not_turned_away(tmp_path):
    """The check must stay narrow — a JSON file goes through to the renderer."""
    import json as _json

    from chester.citymodeltools import _not_cityjson

    cache = tmp_path / "geocache"
    cache.mkdir(parents=True)
    cj = cache / "city.json"
    cj.write_text(_json.dumps({"type": "CityJSON", "version": "1.1",
                               "CityObjects": {}, "vertices": []}), encoding="utf-8")
    assert _not_cityjson(str(cj), "city.json") is None


def test_the_too_heavy_refusal_names_the_3d_way_out_first(tmp_path, monkeypatch):
    """A 3D scene that will not embed still has a 3D route — say it before the 2D one.

    Measured 2026-09-27 (`city3d-html-maximilianstrasse`, 0/3): the `roofs` renderer
    refused 2375 buildings at 6.2 MB — correctly, that is the inline guard. Its message
    offered three exits: narrow the bbox, lower max_points, open QGIS. None of them
    keeps an interactive 3D page in the browser, so the ressort fell back to a flat
    height map and the run failed on the criterion that asks for a self-contained HTML
    rendering the buildings in 3D.

    The exit that works was inside the same tool: `style="blocks"` rendered those same
    2375 buildings in 0.2 s into a 1.7 MB page. A refusal that lists every way out but
    that one buys a wrong decision with a true sentence.
    """
    monkeypatch.setattr(citymodel, "_MAX_INLINE_3D_MB", 1e-9)  # force the guard
    src = tmp_path / "b.city.json"
    citymodel.write_cityjson(_write(tmp_path), str(src), epsg=25832)
    r = citymodel.render_cityjson_html_3d(str(src), str(tmp_path / "big.html"))
    assert r["ok"] is False
    assert 'style="blocks"' in r["reason"], "the 3D fallback has to be named"
    assert r["reason"].index("blocks") < r["reason"].index("QGIS"), "and named first"
    assert "flat map is not a substitute" in r["reason"]


def test_fetch_cityjson_never_writes_cityjson_under_a_vector_name(monkeypatch, tmp_path):
    """The file is CityJSON, so its name has to say so — whatever the caller asked for.

    Measured 2026-09-30 (`heldout-regensburg-tallest-buildings-map`, L+): the agent asked
    for `altstadt_buildings.gpkg` and got CityJSON under that name with `ok: true`. Every
    read of it as a vector layer died on `DataSourceError: Missing or unhandled root type
    object`. Four such files, eighteen fruitless snippets, then a fallback to OSM
    estimates — 40 m for the tallest building in the Altstadt instead of the cathedral's
    107 m, and a FAIL on five of seven criteria. The two neighbouring tools in this
    module had always normalised their suffix; this one was the exception.
    """
    from chester import citymodel, lod2
    from chester.citymodeltools import build_tools

    (tmp_path / "geocache").mkdir(parents=True)
    monkeypatch.setattr(lod2, "download_citygml_tiles", lambda *a, **k: {
        "ok": True, "gml_paths": ["x.gml"], "epsg": 25832, "state": "BY",
        "state_name": "Bayern", "licence": "LoD2 © BY"})
    monkeypatch.setattr(citymodel, "write_cityjson", lambda *a, **k: None)
    monkeypatch.setattr(citymodel, "subset_bbox",
                        lambda *a, **k: {"ok": True, "buildings": 7, "crs": "EPSG:25832"})

    tools = {f.__name__: f for f in build_tools(str(tmp_path))}
    res = tools["fetch_cityjson"](bbox=[12.08, 49.01, 12.11, 49.03],
                                  output_path="altstadt_buildings.gpkg")
    assert res["ok"] is True
    assert res["output"].endswith(".city.json"), res["output"]
    assert not res["output"].endswith(".gpkg")
    assert res["renamed_from"] == "altstadt_buildings.gpkg"
    assert res["format"] == "CityJSON"
    assert "cityjson_to_geopackage" in res["note"], "name the way to a vector layer"
    assert "fetch_lod2" in res["note"], "and the shorter way to flat footprints"


def test_a_cityjson_name_is_left_alone(monkeypatch, tmp_path):
    """Normalising must not rename what was already right."""
    from chester import citymodel, lod2
    from chester.citymodeltools import build_tools

    (tmp_path / "geocache").mkdir(parents=True)
    monkeypatch.setattr(lod2, "download_citygml_tiles", lambda *a, **k: {
        "ok": True, "gml_paths": ["x.gml"], "epsg": 25832, "state": "BY",
        "state_name": "Bayern", "licence": "LoD2 © BY"})
    monkeypatch.setattr(citymodel, "write_cityjson", lambda *a, **k: None)
    monkeypatch.setattr(citymodel, "subset_bbox",
                        lambda *a, **k: {"ok": True, "buildings": 7, "crs": "EPSG:25832"})

    tools = {f.__name__: f for f in build_tools(str(tmp_path))}
    res = tools["fetch_cityjson"](bbox=[12.08, 49.01, 12.11, 49.03],
                                  output_path="modell.cityjson")
    assert res["output"].endswith("modell.cityjson")
    assert "renamed_from" not in res
