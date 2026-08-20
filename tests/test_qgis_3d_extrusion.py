"""A flat layer must not be reported as 3D (qgis_bridge `_show_3d`).

`qgis_show_3d` used to hand every polygon layer a 3D symbol and report success.
For a layer with Z that is right; for a **flat** layer it drew footprints lying on
the ground inside a 3D window and called it a 3D view. The extrusion branch existed
but was dead: `extrusion_height` was never passed by any caller, and even set it was
one constant for the whole layer rather than the per-building height.

Found 2026-08-19 via `city3d-regensburg-dom-height`, where the agent fetched
`fetch_lod2` (flat footprints + `measured_height`), found no way to 3D, and
delivered a 2D choropleth instead.

`chester/qgis_bridge.py` runs *inside* QGIS and cannot be imported into this venv,
so these drive the shipped code through the headless PyQGIS runner. They skip
without a local QGIS, like every other `requires_qgis` test.
"""

from __future__ import annotations

from _util import requires_qgis

from chester.qgis_python import run_pyqgis

# Builds every case in one QGIS process (a run costs ~40 s, so they share it),
# calls the real `_show_3d`, and reports both its verdict and what actually landed
# on each symbol — the return value alone is what used to lie.
_DRIVER = """
import sys
from types import SimpleNamespace

sys.path.insert(0, {repo!r})

from qgis._3d import QgsPolygon3DSymbol
from qgis.core import QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer

from chester.qgis_bridge import LiveBridge

proj = QgsProject.instance()


def _add(name, definition, wkt, fields=None):
    layer = QgsVectorLayer(definition, name, "memory")
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromWkt(wkt))
    for key, value in (fields or {{}}).items():
        feature.setAttribute(key, value)
    layer.dataProvider().addFeature(feature)
    proj.addMapLayer(layer)
    return layer


square = "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))"
flat_h = _add("flat_with_height",
              "Polygon?crs=EPSG:25832&field=measured_height:double",
              square, {{"measured_height": 107.23}})
flat_none = _add("flat_no_height",
                 "Polygon?crs=EPSG:25832&field=name:string", square, {{"name": "x"}})
flat_odd = _add("flat_odd_name",
                "Polygon?crs=EPSG:25832&field=gebaeudehoehe:double",
                square, {{"gebaeudehoehe": 12.5}})
# Only non-height numerics: extruding by these would be nonsense, so it stays flat.
flat_bad = _add("flat_wrong_numbers",
                "Polygon?crs=EPSG:25832&field=area:double&field=osm_id:int",
                square, {{"area": 4200.0, "osm_id": 99}})
zed = _add("has_z", "PolygonZ?crs=EPSG:25832",
           "POLYGONZ((0 0 5, 1 0 5, 1 1 5, 0 1 5, 0 0 5))")

# A real LiveBridge minus __init__ (which would open a TCP server): every method
# exercised below is the shipped one.
bridge = LiveBridge.__new__(LiveBridge)
bridge.iface = SimpleNamespace()
verdict = bridge._show_3d({extra})

symbols = {{}}
for layer in (flat_h, flat_none, flat_odd, flat_bad, zed):
    renderer = layer.renderer3D()
    symbol = renderer.symbol() if renderer else None
    if symbol is None:
        symbols[layer.name()] = None
        continue
    prop = symbol.dataDefinedProperties().property(
        QgsPolygon3DSymbol.Property.ExtrusionHeight)
    symbols[layer.name()] = {{
        "field": prop.field() if prop.isActive() else None,
        "constant": symbol.extrusionHeight(),
    }}

result = {{"verdict": verdict, "symbols": symbols}}
"""


def _run(repo, height_field=None):
    extra = f"height_field={height_field!r}" if height_field else ""
    out = run_pyqgis(_DRIVER.format(repo=str(repo), extra=extra), timeout=300)
    assert out["ok"], out.get("error")
    return out["result"]


@requires_qgis
def test_show_3d_extrudes_per_feature_and_names_what_stayed_flat(tmp_path, request):
    repo = request.config.rootpath
    got = _run(repo)
    verdict, symbols = got["verdict"], got["symbols"]

    # The defect: a height column exists, so the layer must be extruded *by it*.
    assert verdict["extruded"]["flat_with_height"] == "measured_height"
    assert symbols["flat_with_height"]["field"] == "measured_height"
    # An alternate spelling from the allow-list works too.
    assert verdict["extruded"]["flat_odd_name"] == "gebaeudehoehe"

    # A layer with nothing to extrude by is *named*, not silently styled: that is
    # the whole point — the caller can then say "this is not 3D" instead of
    # reporting a successful 3D view over flat polygons.
    assert set(verdict["flat"]) == {"flat_no_height", "flat_wrong_numbers"}
    assert symbols["flat_no_height"]["field"] is None

    # `area`/`osm_id` are numeric and must NOT be mistaken for a height.
    assert symbols["flat_wrong_numbers"]["field"] is None

    # A layer that already carries Z keeps the geometry-clamped path (no extrusion).
    assert "has_z" not in verdict["extruded"] and "has_z" not in verdict["flat"]
    assert symbols["has_z"]["field"] is None

    # Every polygon layer still gets a renderer, as before.
    assert set(verdict["styled_3d"]) >= {"flat_with_height", "flat_no_height", "has_z"}


@requires_qgis
def test_an_explicit_height_field_wins_over_the_allow_list(tmp_path, request):
    """The caller knows the schema better than a name list can."""
    repo = request.config.rootpath
    got = _run(repo, height_field="gebaeudehoehe")
    # `flat_with_height` has `measured_height`, not the requested column, so the
    # allow-list still answers for it; the explicit choice only binds where present.
    assert got["verdict"]["extruded"]["flat_odd_name"] == "gebaeudehoehe"
    assert got["verdict"]["extruded"]["flat_with_height"] == "measured_height"
