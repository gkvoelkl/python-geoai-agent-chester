"""Tests for the headless PyQGIS runner + GeoPyCapability.

`_collect_output_paths` is pure (no QGIS). The runner/capability tests are gated
on a local QGIS via `requires_qgis` — they shell out to QGIS's bundled Python.
"""

import os

from _util import requires_qgis, tools_of

from chester.capabilities.qgis_python import GeoPyCapability, _collect_output_paths


def _ctx():
    """A synthetic RunContext: no conversation, so the search-first gate stands aside.

    The gate refuses a snippet only inside a *run* that never searched. A direct
    call has no conversation to judge, so it must pass — otherwise every unit test
    would have to fake a tool history to exercise unrelated behaviour.
    """
    from types import SimpleNamespace

    return SimpleNamespace(messages=[], run_id=None)

# ── unit: output-path collection (no QGIS) ──────────────────────────────────


def test_collect_paths_from_string(tmp_path):
    f = tmp_path / "out.gpkg"
    f.write_text("x")
    assert _collect_output_paths(str(f), str(tmp_path)) == [str(f)]


def test_collect_paths_joins_relative_to_cache_dir(tmp_path):
    (tmp_path / "out.gpkg").write_text("x")
    # A bare filename (the snippet's CWD is the cache dir) resolves to the file.
    assert _collect_output_paths("out.gpkg", str(tmp_path)) == [str(tmp_path / "out.gpkg")]


def test_collect_paths_from_list_and_dict(tmp_path):
    a = tmp_path / "a.gpkg"
    b = tmp_path / "b.tif"
    a.write_text("x")
    b.write_text("x")
    assert set(_collect_output_paths([str(a), str(b)], str(tmp_path))) == {str(a), str(b)}
    assert set(_collect_output_paths({"OUTPUT": str(a), "extra": [str(b)]}, str(tmp_path))) == {
        str(a),
        str(b),
    }


def test_collect_paths_ignores_non_files(tmp_path):
    assert _collect_output_paths("does_not_exist.gpkg", str(tmp_path)) == []
    assert _collect_output_paths(42, str(tmp_path)) == []


# ── integration: the runner via the capability (needs QGIS) ─────────────────


@requires_qgis
def test_qgis_python_computes_and_captures_stdout(tmp_path):
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_ctx(), code="print('hi'); result = 2 + 40")
    assert res["ok"] is True
    assert res["result"] == 42
    assert "hi" in res["stdout"]


@requires_qgis
def test_qgis_python_error_returns_traceback(tmp_path):
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_ctx(), code="raise ValueError('boom')")
    assert res["ok"] is False
    assert "ValueError" in res["error"]
    assert "hint" not in res, "ein echter Snippet-Fehler braucht keine Werkzeugliste"


@requires_qgis
def test_qgis_python_namespace_holds_qgis_core_without_an_import(tmp_path):
    """The console-style snippet must run: `NameError: QgsVectorLayer is not
    defined` cost a model turn in a benchmark run, for a name that was one
    binding away."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    code = (
        "lyr = QgsVectorLayer('Point?crs=EPSG:25832&field=id:integer', 'p', 'memory')\n"
        "result = {'valid': lyr.isValid(), 'geom_type': QgsWkbTypes.PointGeometry}\n"
    )
    res = tool(_ctx(), code=code)
    assert res["ok"] is True, res.get("error")
    assert res["result"]["valid"] is True


@requires_qgis
def test_qgis_python_points_at_the_named_tools_when_a_name_is_missing(tmp_path):
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_ctx(), code="QgsVectorLayer('x.gpkg', 'l', 'ogr').setDessicated(True)")
    assert res["ok"] is False and "AttributeError" in res["error"]
    assert "vector_filter" in res["hint"] and "already in the namespace" in res["hint"]


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
    res = tool(_ctx(), code=code)
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
    assert tool(_ctx(), code=make)["ok"] is True
    # Second run addresses it by the workspace-prefixed path the other tools emit;
    # resolve_path must make it a valid layer (not the doubled/invalid path).
    read = (
        "from qgis.core import QgsVectorLayer\n"
        "p = '.chester/workspace/geocache/pts.gpkg'\n"
        "lyr = QgsVectorLayer(resolve_path(p), 'in', 'ogr')\n"
        "result = {'valid': lyr.isValid(), 'count': lyr.featureCount()}\n"
    )
    res = tool(_ctx(), code=read)
    assert res["ok"] is True, res.get("error")
    assert res["result"] == {"valid": True, "count": 1}


# ── search before you write code ─────────────────────────────────────────────


def _run_ctx(tool_names):
    """A RunContext whose run already returned from these tools, in order."""
    from types import SimpleNamespace

    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    parts = [
        ToolReturnPart(tool_name=name, content={"ok": True}, tool_call_id=f"c{i}")
        for i, name in enumerate(tool_names)
    ]
    req = ModelRequest(parts=parts)
    run_id = None
    try:
        req.run_id = "R1"
        run_id = "R1"
    except Exception:  # noqa: BLE001 - older message models degrade to "all messages"
        pass
    return SimpleNamespace(messages=[req], run_id=run_id)


def test_a_snippet_without_any_search_is_refused(tmp_path):
    """Advice did not work; the order is enforced instead.

    "Last resort, not first reach" stood in the docstring, in `_ERROR_HINT` and in
    the instructions — and `qgis_python` stayed the most-called tool of the bank
    (96 calls over 24 runs). On 2026-08-23 `viewpoints-above-400m` wrote **eleven
    consecutive** snippets for what `native:rastersampling` does in one call, and
    the one search it made that run was for "hillshade".
    """
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_run_ctx(["geocode", "osm_features"]), code="result = 1")
    assert res["ok"] is False
    assert "no algorithm search happened" in res["error"]
    assert "qgis_search" in res["error"]
    # Chester's own tools come first in the redirect. When they did not, the refusal
    # sent `dop-ndvi-no-nir-bayern` past `spectral_index` into gdal:rastercalculator,
    # which underflowed the uint16 bands and silently wrecked the NDVI (2026-08-25).
    assert "spectral_index" in res["error"]
    assert res["error"].index("spectral_index") < res["error"].index("qgis_search")


@requires_qgis
def test_a_snippet_runs_once_the_run_has_searched(tmp_path):
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_run_ctx(["qgis_search", "osm_features"]), code="result = 40 + 2")
    assert res["ok"] is True and res["result"] == 42


@requires_qgis
def test_qgis_describe_counts_as_looking_too(tmp_path):
    """Naming an algorithm id and asking for its parameters is the same act."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_run_ctx(["qgis_describe"]), code="result = 'ok'")
    assert res["ok"] is True


@requires_qgis
def test_qvariant_is_in_the_namespace_without_an_import(tmp_path):
    """QGIS 4.2 is built on Qt6, so the habitual PyQt5 import raises — and a field
    cannot be added without QVariant. Binding it removes the question entirely."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_run_ctx(["qgis_search"]), code="result = str(QVariant.Double)")
    assert res["ok"] is True and "Double" in res["result"]


def _refusal(i):
    """A previous qgis_python return that was itself a refusal."""
    from pydantic_ai.messages import ToolReturnPart

    from chester.capabilities.qgis_python import _REFUSAL_MARKER

    return ToolReturnPart(
        tool_name="qgis_python",
        content={"ok": False, "error": f"{_REFUSAL_MARKER}. QGIS ships ~761 algorithms…"},
        tool_call_id=f"r{i}",
    )


def _ctx_with(parts):
    from types import SimpleNamespace

    from pydantic_ai.messages import ModelRequest

    req = ModelRequest(parts=parts)
    run_id = None
    try:
        req.run_id = "R1"
        run_id = "R1"
    except Exception:  # noqa: BLE001
        pass
    return SimpleNamespace(messages=[req], run_id=run_id)


@requires_qgis
def test_the_refusal_gives_way_after_two_tries(tmp_path):
    """A gate without a ceiling can loop, and this project has already lost a run
    to a loop that ended at the request limit. Two refusals were enough for the
    local model to route around (2026-08-23); the third call runs."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    ctx = _ctx_with([_refusal(1), _refusal(2)])
    res = tool(ctx, code="result = 40 + 2")
    assert res["ok"] is True and res["result"] == 42


def test_the_second_try_is_still_refused(tmp_path):
    """The ceiling must not weaken the first push-back."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_ctx_with([_refusal(1)]), code="result = 1")
    assert res["ok"] is False and "no algorithm search happened" in res["error"]
