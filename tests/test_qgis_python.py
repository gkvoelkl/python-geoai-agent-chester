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


#: Ein Schnipsel, der in die **Zuständigkeit** des Wächters fällt. Er greift nur bei
#: Geoverarbeitung — `os.listdir` oder eine Kopfzeile lesen geht ihn nichts an
#: (`_is_geoprocessing`). Die Reihenfolge-Tests hier prüfen *wann* er abweist, nicht
#: *ob* er zuständig ist; sie brauchen deshalb einen räumlichen Schnipsel.
GEO = 'crs = "EPSG:25832"\n'


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
    # Bis zum 2026-09-01 stand hier `qgis_search` — die Abweisung **forderte** eine
    # Suche. Sie fährt sie jetzt selbst (siehe die drei Tests am Dateiende), also
    # nennt sie den generischen Weg als `qgis_run`. Was unverändert gilt: Chesters
    # eigene Werkzeuge stehen **vor** dem generischen. Als sie das nicht taten, schickte
    # die Abweisung `dop-ndvi-no-nir-bayern` an `spectral_index` vorbei in
    # gdal:rastercalculator, was die uint16-Bänder unterlaufen ließ und den NDVI still
    # ruinierte (2026-08-25).
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
    """Ein Wächter ohne Obergrenze kann kreisen — und dieses Projekt hat schon einen
    Lauf an eine Schleife verloren, die am Anfragelimit endete.

    Die Grenze lag bei zwei und wurde am 2026-09-01 auf drei gesetzt, als die Sperre
    nach jedem Schnipsel wieder scharf wurde: Gezählt wird seither über den **ganzen
    Lauf**, nicht je Serie. Der Anlass war ein Fall, in dem der empfohlene Weg selbst
    abstürzte — dort wies die Sperre zehnmal ab und half kein einziges Mal."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    ctx = _ctx_with([_refusal(1), _refusal(2), _refusal(3)])
    res = tool(ctx, code="result = 40 + 2")
    assert res["ok"] is True and res["result"] == 42


def test_the_second_try_is_still_refused(tmp_path):
    """The ceiling must not weaken the first push-back."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_ctx_with([_refusal(1)]), code=GEO + "result = 1")
    assert res["ok"] is False and "no algorithm search happened" in res["error"]


def test_the_gate_re_arms_after_a_snippet_has_run(tmp_path):
    """Eine Suche hebt die Sperre für **einen** Schnipsel auf, nicht für den Lauf.

    Aus dem Betrieb, 2026-08-27 (Sitzung `553e7483`): eine Suche nach „buffer",
    danach zwölf weitere handgeschriebene PyQGIS-Blöcke, die eine Punktebene aus
    vier Adressen zusammensetzten und am Ende ein leeres Raster erzeugten. Nach
    `gdal:rasterize` — erster Treffer von `qgis_search("rasterize")` — hat der Lauf
    nie gesucht, weil er nicht mehr musste.
    """
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    # Suche → Schnipsel gelaufen → jetzt wieder scharf.
    res = tool(_run_ctx(["qgis_search", "qgis_python"]), code=GEO + "result = 1")
    assert res["ok"] is False
    assert "no algorithm search happened" in res["error"]


@requires_qgis
def test_a_fresh_search_lifts_it_again(tmp_path):
    """Wer erneut sucht, darf erneut schreiben — die Sperre ist keine Quote."""
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_run_ctx(["qgis_search", "qgis_python", "qgis_search"]), code="result = 40 + 2")
    assert res["ok"] is True and res["result"] == 42


def test_the_hint_points_at_the_qgis_documentation():
    """Der Agent soll nachschlagen können statt zu raten — die zwei Anlaufstellen."""
    from chester.capabilities.qgis_python import _ERROR_HINT

    assert "processing_algs" in _ERROR_HINT      # Algorithmenverzeichnis
    assert "qgis.org/pyqgis" in _ERROR_HINT      # PyQGIS-API
    assert "qgis_rasterize" in _ERROR_HINT       # das Werkzeug für genau diesen Fall


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
    """Die Sperre soll auskunftsfähig sein, nicht nur streng.

    Gemessen 2026-09-01 (`height-gini`, Test-Level 2): **ein** Aufruf, ein fertiges
    Snippet, abgewiesen — und die zweite Runde passte nicht mehr in den Zeitdeckel.
    Für einen Gini-Koeffizienten gibt es in QGIS kein Verfahren, die verlangte Suche
    wäre also garantiert leer ausgegangen. Ein Wächter, der eine Auskunft erzwingt,
    die er selbst geben kann, kostet nur Zeit.
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
    """Nichts gefunden ist **kein** Freibrief — die Wörter stammen vom Modell.

    Gemessen 2026-09-05 (`join-leading-zero-ags`, Test-Level 2): Die Suchwörter
    kommen aus den Bezeichnern des Schnipsels, hier `v_layer, temp_layer,
    einwohner`. Nichts gefunden, und die Abweisung schloss mit „so a snippet is the
    right route here" — obwohl die Aufgabe ein **Join** war und `qgis_search("join")`
    `native:joinattributestable` liefert. Aus der Auskunft war eine Erlaubnis
    geworden; der Lauf schrieb den Join von Hand bis zum Zeitdeckel.
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
    """„Call it again and it will run" muss wahr sein.

    Bis zum 2026-09-01 war es das nicht: Der zweite und dritte Versuch bekamen
    denselben Text erneut, und ein Lauf verlor drei Runden an einen Wächter, der sich
    nicht öffnen ließ — bei einem Zeitdeckel, in den zwei Runden passen.
    """
    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    res = tool(_ctx_with([_searched_refusal(1)]), code="result = 40 + 2")
    assert res["ok"] is True and res["result"] == 42


@requires_qgis
def test_a_snippet_that_is_not_geoprocessing_is_none_of_the_guards_business(tmp_path):
    """`os.listdir` und eine Kopfzeile lesen gehen den Wächter nichts an.

    Gemessen 2026-09-05 (`points-from-a-table`, Test-Level 2): Drei von fünf
    `qgis_python`-Aufrufen wurden abgewiesen, und kein einziger davon war
    Geoverarbeitung — `os.listdir('.')` und zweimal die Kopfzeile einer CSV. Für
    keinen davon gibt es einen Algorithmus zu finden; die Aufforderung `qgis_search`
    war gegenstandslos. Schlimmer: Der Agent lernte daraus, eine Runde an einen
    Wegwerf-Schnipsel (`result = 1 + 1`) zu hängen, um die Sperre zu öffnen — und
    verbrauchte damit den Freipass, den der eigentliche Schnipsel gebraucht hätte.
    """
    from chester.capabilities.qgis_python import _REFUSAL_MARKER

    tool = tools_of(GeoPyCapability(workspace=str(tmp_path)))["qgis_python"]
    for code in ("import os\nresult = os.listdir('.')",
                 "with open('adressen.csv') as f:\n    result = f.readline()",
                 "result = 1 + 1"):
        res = tool(_run_ctx(["geocode"]), code=code)
        # Der Schnipsel darf scheitern (die CSV liegt hier nicht) — er darf nur nicht
        # an der Sperre scheitern.
        assert _REFUSAL_MARKER not in str(res.get("error", "")), code


def test_the_remit_covers_what_the_guard_was_built_for():
    """Die Zuständigkeit darf nicht die Fälle mitverlieren, für die es sie gibt."""
    from chester.capabilities.qgis_python import _is_geoprocessing

    assert _is_geoprocessing("lyr = QgsVectorLayer(p, 'l', 'ogr')")
    assert _is_geoprocessing("out = processing.run('native:buffer', {...})")
    assert _is_geoprocessing("import geopandas as gpd\ngdf = gpd.read_file(p)")
    assert _is_geoprocessing("crs = 'EPSG:25832'")
    # und die drei aus dem Lauf, die es nicht sind
    assert not _is_geoprocessing("import os\nresult = os.listdir('.')")
    assert not _is_geoprocessing("with open('a.csv') as f:\n    result = f.readline()")
    assert not _is_geoprocessing("result = 1 + 1")


def test_a_huge_return_is_capped_before_it_reaches_the_conversation():
    """Ein `print` darf das Kontextfenster nicht aufbrauchen.

    Gemessen 2026-09-05 (`supermarket-accessibility-choropleth`): Der Agent debuggte
    einen leeren Clip mit `print(feature.geometry().asWkt())`. Die Landkreisgrenze
    sind **451.593 Zeichen** ≈ 113k Token, 43 % des Fensters aus einer Zeile. Nach
    dem zweiten solchen Aufruf endete der Lauf nach 29 Minuten an
    `input length (745882 tokens) exceeds the model's maximum context length`.
    Eine Geometrie auszudrucken ist beim Debuggen richtig — sie ungekürzt
    zurückzugeben ist der Fehler des Werkzeugs, nicht des Modells.
    """
    from chester.capabilities.qgis_python import _MAX_RETURN_CHARS, _clipped

    wkt = "MultiPolygon (((" + "694409.88 5433145.85, " * 20000 + ")))"
    assert len(wkt) > 400_000
    out = _clipped(wkt)
    assert len(out) < _MAX_RETURN_CHARS + 400
    assert out.startswith("MultiPolygon ((("), "der Anfang muss lesbar bleiben"
    assert "characters cut" in out and "vector_info" in out


def test_the_cap_leaves_normal_returns_alone():
    """Kein Eingriff in das, was ohnehin passt — und Nicht-Strings bleiben, was sie sind."""
    from chester.capabilities.qgis_python import _clipped

    assert _clipped("ok") == "ok"
    assert _clipped({"count": 4}) == {"count": 4}
    assert _clipped(None) is None
    assert _clipped(42) == 42
