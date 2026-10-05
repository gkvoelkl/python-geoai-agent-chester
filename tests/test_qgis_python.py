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


#: A snippet that falls within the guard's **remit**. It only applies to geoprocessing —
#: `os.listdir` or reading a header line are none of its business (`_is_geoprocessing`).
#: The ordering tests here check *when* it refuses, not *whether* it is responsible; so
#: they need a spatial snippet.
GEO = 'crs = "EPSG:25832"\n'


@requires_qgis
def test_a_snippet_without_any_search_is_refused(tmp_path):
    """Advice did not work; the order is enforced instead.

    "Last resort, not first reach" stood in the docstring, in `_ERROR_HINT` and in
    the instructions — and `qgis_python` stayed the most-called tool of the bank
    (96 calls over 24 runs). On 2026-08-23 `viewpoints-above-400m` wrote **eleven
    consecutive** snippets for what `native:rastersampling` does in one call, and
    the one search it made that run was for "hillshade".
    """
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_run_ctx(["geocode", "osm_features"]), code=GEO + "result = 1")
    assert res["ok"] is False
    assert "no algorithm search happened" in res["error"]
    # Until 2026-09-01 this said `qgis_search` — the refusal **demanded** a search. It now
    # runs the search itself (see the three tests at the end of the file), so it names the
    # generic route as `qgis_run`. What still holds: Chester's own tools stand **before**
    # the generic one. When they did not, the refusal sent `dop-ndvi-no-nir-bayern` past
    # `spectral_index` into gdal:rastercalculator, which underflowed the uint16 bands and
    # silently ruined the NDVI (2026-08-25).
    assert "spectral_index" in res["error"]
    assert res["error"].index("spectral_index") < res["error"].index("qgis_run")


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
def test_the_refusal_gives_way_after_three_tries(tmp_path):
    """A guard without an upper limit can circle — and this project has already lost a
    run to a loop that ended at the request limit.

    The limit was two and was set to three on 2026-09-01, when the gate re-armed after
    every snippet: since then the count runs over the **whole run**, not per series. The
    occasion was a case where the recommended route itself crashed — there the gate
    refused ten times and did not help once."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    ctx = _ctx_with([_refusal(1), _refusal(2), _refusal(3)])
    res = tool(ctx, code="result = 40 + 2")
    assert res["ok"] is True and res["result"] == 42


@requires_qgis
def test_the_second_try_is_still_refused(tmp_path):
    """The ceiling must not weaken the first push-back."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_ctx_with([_refusal(1)]), code=GEO + "result = 1")
    assert res["ok"] is False and "no algorithm search happened" in res["error"]


@requires_qgis
def test_the_gate_re_arms_after_a_snippet_has_run(tmp_path):
    """A search lifts the gate for **one** snippet, not for the run.

    From practice, 2026-08-27 (session `553e7483`): one search for "buffer", then twelve
    more hand-written PyQGIS blocks that assembled a point layer from four addresses and
    produced an empty raster in the end. After `gdal:rasterize` — the first hit of
    `qgis_search("rasterize")` — the run never searched, because it no longer had to.
    """
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    # Suche → Schnipsel gelaufen → jetzt wieder scharf.
    res = tool(_run_ctx(["qgis_search", "qgis_python"]), code=GEO + "result = 1")
    assert res["ok"] is False
    assert "no algorithm search happened" in res["error"]


@requires_qgis
def test_a_fresh_search_lifts_it_again(tmp_path):
    """Whoever searches again may write again — the gate is not a quota."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_run_ctx(["qgis_search", "qgis_python", "qgis_search"]), code="result = 40 + 2")
    assert res["ok"] is True and res["result"] == 42


def test_the_hint_points_at_the_qgis_documentation():
    """The agent should be able to look things up instead of guessing — the two places."""
    from chester.capabilities.qgis_python import _ERROR_HINT

    assert "processing_algs" in _ERROR_HINT      # Algorithmenverzeichnis
    assert "qgis.org/pyqgis" in _ERROR_HINT      # PyQGIS-API
    assert "vector_clip, rasterize" in _ERROR_HINT  # the tool for exactly this case


def _searched_refusal(i):
    """Eine frühere Abweisung, die die Suche **selbst** gefahren hat."""
    from pydantic_ai.messages import ToolReturnPart

    from chester.capabilities.qgis_python import _REFUSAL_MARKER, _SEARCHED_MARKER

    return ToolReturnPart(
        tool_name="qgis_python",
        content={"ok": False, "error": f"{_REFUSAL_MARKER}. …",
                 "searched": f"{_SEARCHED_MARKER}: gini, heights"},
        tool_call_id=f"s{i}",
    )


@requires_qgis
def test_the_refusal_brings_the_search_along(tmp_path):
    """The gate should be informative, not just strict.

    Measured 2026-09-01 (`height-gini`, Test-Level 2): **one** call, a finished snippet,
    refused — and the second round no longer fit into the time limit. QGIS has no
    algorithm for a Gini coefficient, so the demanded search was bound to come up empty.
    A guard that forces information it can give itself only costs time.
    """
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_run_ctx(["geocode"]),
               code='layer = QgsVectorLayer(p, "b", "ogr")\n'
                    'ras = rasterize(layer, burn=1)\nresult = ras')
    assert res["ok"] is False
    ids = [c["id"] for c in res["candidates"]]
    assert any("rasterize" in i for i in ids), ids
    assert "searched" in res, "die Abweisung muss ausweisen, dass sie gesucht hat"


@requires_qgis
def test_a_nulltreffer_does_not_endorse_the_snippet(tmp_path):
    """Nothing found is **no** licence — the words come from the model.

    Measured 2026-09-05 (`join-leading-zero-ags`, Test-Level 2): the search words come
    from the snippet's identifiers, here `v_layer, temp_layer, einwohner`. Nothing found,
    and the refusal closed with "so a snippet is the right route here" — although the
    task was a **join** and `qgis_search("join")` returns `native:joinattributestable`.
    The information had turned into a permission; the run wrote the join by hand until
    the time limit.
    """
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_run_ctx(["geocode"]),
               code='v_layer = QgsVectorLayer(p, "g", "ogr")\n'
                    'temp_layer = einwohner\nresult = {"n": len(v_layer)}')
    assert res["ok"] is False and res["candidates"] == []
    err = res["error"]
    assert "right route" not in err, "der Nulltreffer darf den Schnipsel nicht adeln"
    assert "says little" in err, "die Abweisung muss die schwache Evidenz benennen"
    assert "qgis_search" in err and "join" in err, "sie muss den Ausweg zeigen"
    assert "again" in err, "und die zweite Runde muss weiterhin laufen dürfen"


@requires_qgis
def test_one_refusal_is_enough_when_it_searched(tmp_path):
    """"Call it again and it will run" must be true.

    Until 2026-09-01 it was not: the second and third attempts got the same text again,
    and a run lost three rounds to a guard that would not open — with a time limit that
    fits two rounds.
    """
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_ctx_with([_searched_refusal(1)]), code="result = 40 + 2")
    assert res["ok"] is True and res["result"] == 42


@requires_qgis
def test_a_snippet_that_is_not_geoprocessing_is_none_of_the_guards_business(tmp_path):
    """`os.listdir` and reading a header line are none of the guard's business.

    Measured 2026-09-05 (`points-from-a-table`, Test-Level 2): three of five
    `qgis_python` calls were refused, and not one of them was geoprocessing —
    `os.listdir('.')` and twice the header line of a CSV. There is no algorithm to find
    for any of them; the demand for `qgis_search` was pointless. Worse: the agent learned
    to spend a round on a throwaway snippet (`result = 1 + 1`) to open the gate — and so
    used up the pass the real snippet would have needed.
    """
    from chester.capabilities.qgis_python import _REFUSAL_MARKER

    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    for code in ("import os\nresult = os.listdir('.')",
                 "with open('adressen.csv') as f:\n    result = f.readline()",
                 "result = 1 + 1"):
        res = tool(_run_ctx(["geocode"]), code=code)
        # The snippet may fail (the CSV is not here) — it just must not fail at the
        # gate.
        assert _REFUSAL_MARKER not in str(res.get("error", "")), code


def test_the_remit_covers_what_the_guard_was_built_for():
    """The remit must not lose the cases it exists for."""
    from chester.capabilities.qgis_python import _is_geoprocessing

    assert _is_geoprocessing("lyr = QgsVectorLayer(p, 'l', 'ogr')")
    assert _is_geoprocessing("out = processing.run('native:buffer', {...})")
    assert _is_geoprocessing("import geopandas as gpd\ngdf = gpd.read_file(p)")
    assert _is_geoprocessing("crs = 'EPSG:25832'")
    # and the three from the run that are not
    assert not _is_geoprocessing("import os\nresult = os.listdir('.')")
    assert not _is_geoprocessing("with open('a.csv') as f:\n    result = f.readline()")
    assert not _is_geoprocessing("result = 1 + 1")
