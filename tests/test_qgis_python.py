"""Tests for the headless PyQGIS runner + GeoPyCapability.

`_collect_output_paths` is pure (no QGIS). The runner/capability tests are gated
on a local QGIS via `requires_qgis` — they shell out to QGIS's bundled Python.
"""

import os

from _util import requires_qgis, tools_of

from chester.capabilities.qgis_python import GeoPyCapability, _collect_output_paths

# ── unit: output-path collection (no QGIS) ──────────────────────────────────

def test_collect_paths_from_string(tmp_path):
    f = tmp_path / "out.gpkg"
    f.write_text("x")
    assert _collect_output_paths(str(f), str(tmp_path)) == [str(f)]


def test_collect_paths_joins_relative_to_cache_dir(tmp_path):
    (tmp_path / "out.gpkg").write_text("x")
    # A bare filename (the snippet's CWD is the cache dir) resolves to the file.
    assert _collect_output_paths("out.gpkg", str(tmp_path)) == [
        str(tmp_path / "out.gpkg")
    ]


def test_collect_paths_from_list_and_dict(tmp_path):
    a = tmp_path / "a.gpkg"
    b = tmp_path / "b.tif"
    a.write_text("x")
    b.write_text("x")
    assert set(_collect_output_paths([str(a), str(b)], str(tmp_path))) == {str(a), str(b)}
    assert set(
        _collect_output_paths({"OUTPUT": str(a), "extra": [str(b)]}, str(tmp_path))
    ) == {str(a), str(b)}


def test_collect_paths_ignores_non_files(tmp_path):
    assert _collect_output_paths("does_not_exist.gpkg", str(tmp_path)) == []
    assert _collect_output_paths(42, str(tmp_path)) == []


# ── integration: the runner via the capability (needs QGIS) ─────────────────

@requires_qgis
def test_qgis_python_computes_and_captures_stdout(tmp_path):
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(code="print('hi'); result = 2 + 40")
    assert res["ok"] is True
    assert res["result"] == 42
    assert "hi" in res["stdout"]


@requires_qgis
def test_qgis_python_error_returns_traceback(tmp_path):
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(code="raise ValueError('boom')")
    assert res["ok"] is False
    assert "ValueError" in res["error"]


@requires_qgis
def test_qgis_python_output_lands_in_cache_with_provenance(tmp_path):
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    code = (
        "import processing\n"
        "from qgis.core import (QgsVectorLayer, QgsFeature, QgsGeometry,\n"
        "    QgsPointXY)\n"
        "lyr = QgsVectorLayer('Point?crs=EPSG:25832&field=id:integer', 'p', 'memory')\n"
        "f = QgsFeature(); f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(5e5, 54e5)))\n"
        "f.setAttributes([1]); lyr.dataProvider().addFeature(f); lyr.updateExtents()\n"
        "out = processing.run('native:buffer',\n"
        "    {'INPUT': lyr, 'DISTANCE': 100.0, 'OUTPUT': 'buffered.gpkg'})\n"
        "result = out['OUTPUT']\n"
    )
    res = tool(code=code)
    assert res["ok"] is True, res.get("error")
    assert res["outputs"], "expected the buffered output to be collected"
    out = res["outputs"][0]
    # confined to the workspace cache, and provenance-stamped as a chester output
    assert os.path.isfile(out)
    assert str(tmp_path) in out and "geocache" in out
    assert os.path.isfile(out + ".meta.json")


@requires_qgis
def test_qgis_python_resolve_path_reads_prefixed_cache_path(tmp_path):
    """A cache file addressed by the `.chester/workspace/geocache/…` path other
    tools print is loadable via the injected `resolve_path` helper — the footgun
    that silently produced an invalid layer in a real run."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    # First run writes a layer into the cache (bare name → CWD = cache dir).
    make = (
        "from qgis.core import (QgsVectorLayer, QgsFeature, QgsGeometry,\n"
        "    QgsPointXY, QgsVectorFileWriter, QgsProject)\n"
        "lyr = QgsVectorLayer('Point?crs=EPSG:25832&field=id:integer', 'p', 'memory')\n"
        "f = QgsFeature(); f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(5e5, 54e5)))\n"
        "f.setAttributes([1]); lyr.dataProvider().addFeature(f); lyr.updateExtents()\n"
        "QgsVectorFileWriter.writeAsVectorFormatV3(lyr, 'pts.gpkg',\n"
        "    QgsProject.instance().transformContext(),\n"
        "    QgsVectorFileWriter.SaveVectorOptions())\n"
        "result = 'pts.gpkg'\n"
    )
    assert tool(code=make)["ok"] is True
    # Second run addresses it by the workspace-prefixed path the other tools emit;
    # resolve_path must make it a valid layer (not the doubled/invalid path).
    read = (
        "from qgis.core import QgsVectorLayer\n"
        "p = '.chester/workspace/geocache/pts.gpkg'\n"
        "lyr = QgsVectorLayer(resolve_path(p), 'in', 'ogr')\n"
        "result = {'valid': lyr.isValid(), 'count': lyr.featureCount()}\n"
    )
    res = tool(code=read)
    assert res["ok"] is True, res.get("error")
    assert res["result"] == {"valid": True, "count": 1}
