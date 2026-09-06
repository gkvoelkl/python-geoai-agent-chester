"""A QGIS algorithm that drops a geometry type must say so (qgis capability).

`native:clip` and its siblings write **one** geometry type. Handed a layer that
holds several they keep the first and discard the rest without a word — the run
reports `ok: true` and a plausible feature count. Measured 2026-08-19 on
`supermarket-accessibility-choropleth`: 247 OSM supermarkets in (109 points, 138
polygons), 107 out. Because OSM draws the *larger* shops as building polygons,
the survivors were the small ones, and the finished choropleth reported 18
supermarkets for a district that has 80.

Two properties are pinned here, and the second matters as much as the first: the
warning must not fire for an algorithm that changes geometry **by design**.
`buffer` (point→polygon), `centroids` (polygon→point) and `countpointsinpolygon`
(returns the polygons) all "lose" an input type legitimately, and a field that
cries wolf on correct results teaches the model to skip reading it.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from _util import requires_qgis, tools_of
from shapely.geometry import Point, box

from chester.capabilities.qgis import QgisToolboxCapability


@pytest.fixture
def layers(tmp_path):
    """A mixed point+polygon layer, a clean polygon layer, and a clip mask."""
    mixed = gpd.GeoDataFrame(
        {"kind": ["p", "p", "a", "a"]},
        geometry=[Point(10, 10), Point(20, 20),
                  box(30, 30, 40, 40), box(50, 50, 60, 60)],
        crs="EPSG:25832",
    )
    mixed.to_file(tmp_path / "mixed.gpkg", driver="GPKG")
    gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[box(0, 0, 45, 45), box(46, 46, 100, 100)],
        crs="EPSG:25832",
    ).to_file(tmp_path / "areas.gpkg", driver="GPKG")
    gpd.GeoDataFrame(
        {"name": ["mask"]}, geometry=[box(0, 0, 100, 100)], crs="EPSG:25832"
    ).to_file(tmp_path / "mask.gpkg", driver="GPKG")
    return tools_of(QgisToolboxCapability(workspace=str(tmp_path)))


@requires_qgis
def test_a_clip_that_drops_a_geometry_type_says_so(layers, tmp_path):
    result = layers["qgis_clip"](input_path="mixed.gpkg", overlay_path="mask.gpkg",
                                 output_path="clipped.gpkg")
    assert result["ok"]
    warning = result.get("warning") or ""
    # The count of what was lost is the load-bearing part: "some features are
    # missing" is ignorable, "2× Polygon was dropped" is not.
    assert "Polygon" in warning and "dropped" in warning
    assert "centroids" in warning, "der Ausweg muss mitgeliefert werden"


@requires_qgis
def test_a_clean_layer_draws_no_warning(layers, tmp_path):
    result = layers["qgis_clip"](input_path="areas.gpkg", overlay_path="mask.gpkg",
                                 output_path="clean.gpkg")
    assert result["ok"] and "warning" not in result


@requires_qgis
def test_an_algorithm_that_changes_geometry_by_design_draws_no_warning(layers):
    """A buffer turns points into polygons — that is the job, not a loss."""
    result = layers["qgis_buffer"](input_path="areas.gpkg", distance=1,
                                   output_path="buffered.gpkg")
    assert result["ok"] and "warning" not in result


@requires_qgis
def test_a_buffer_over_a_mixed_layer_that_already_holds_multipolygons(tmp_path):
    """The case the test above misses — and it cost a real run 11,3 % of its answer.

    The guard used to infer "did this algorithm change the geometry type?" from the
    data: no warning when the output holds a type the input lacked. Handed the 84
    Regensburg schools (`buffer-schools-500m`, 2026-08-23) that inference broke —
    among Points and Polygons there was **one MultiPolygon**, so a dissolved buffer
    to MultiPolygon looked type-preserving and the warning fired: "25× Point, 58×
    Polygon dropped … missing those features entirely". Nothing was missing. The
    agent believed it, switched to centroids and re-buffered: 29,867 → 26,492 km².
    Whether an algorithm constructs its output type is a property of the algorithm,
    not of the data that happened to go in.
    """
    from shapely.geometry import MultiPolygon

    mixed = gpd.GeoDataFrame(
        {"kind": ["p", "a", "m"]},
        geometry=[
            Point(10, 10),
            box(30, 30, 40, 40),
            MultiPolygon([box(50, 50, 55, 55), box(60, 60, 65, 65)]),
        ],
        crs="EPSG:25832",
    )
    mixed.to_file(tmp_path / "mixed_multi.gpkg", driver="GPKG")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))

    result = tools["qgis_buffer"](
        input_path="mixed_multi.gpkg", distance=500, dissolve=True,
        output_path="buffered_multi.gpkg",
    )
    assert result["ok"]
    assert "warning" not in result, (
        "ein Puffer erzeugt Polygone aus allem — das ist die Aufgabe, kein Verlust"
    )


@requires_qgis
def test_count_points_in_polygon_is_not_accused_of_losing_its_input(layers):
    """It returns the POLYGONS layer; every input type is "missing" by design.

    It also counts polygon features, contrary to what its name suggests — checked
    against the same mixed layer, so this test would catch a future QGIS that
    silently stopped doing that.
    """
    result = layers["qgis_run"](
        algorithm_id="native:countpointsinpolygon",
        parameters={"POINTS": "mixed.gpkg", "POLYGONS": "areas.gpkg",
                    "FIELD": "n", "OUTPUT": "counted.gpkg"})
    assert result["ok"], result
    assert "warning" not in result


@requires_qgis
def test_points_and_polygons_resolve_as_paths(layers):
    """`POINTS`/`POLYGONS` were missing from the path-key set.

    Unresolved, qgis_process answered "Could not load source layer for POLYGONS:
    … not found" — a path bug phrased as a missing file, which sent the model
    hunting through `list_directory` for four turns.
    """
    result = layers["qgis_run"](
        algorithm_id="native:countpointsinpolygon",
        parameters={"POINTS": "mixed.gpkg", "POLYGONS": "areas.gpkg",
                    "FIELD": "n", "OUTPUT": "resolved.gpkg"})
    assert result["ok"], result


def test_intersection_says_when_the_result_is_the_overlays_shapes(tmp_path):
    """Polygone ∩ Punkte **ist** Punkte — und liest sich wie das gewollte Ergebnis.

    Gemessen 2026-09-01 (`map-then-geotiff`, Schritt 1): Gefragt waren die
    **Grundflächen** von vier Regensburger Adressen. Der Agent lud die Gebäude,
    verschnitt sie mit den geokodierten Punkten und zeichnete das Ergebnis — vier
    Kreise. Die Rückgabe stimmte in allem, worauf man schaut: `ok: true`, vier
    Objekte, `building=yes` in den Spalten. Nur die Polygone waren weg.
    """
    from chester.capabilities.qgis import _swapped_geometry_warning

    buildings = tmp_path / "b.gpkg"
    points = tmp_path / "p.gpkg"
    out = tmp_path / "out.gpkg"
    gpd.GeoDataFrame({"building": ["yes"] * 2},
                     geometry=[box(0, 0, 2, 2), box(3, 3, 5, 5)],
                     crs="EPSG:25832").to_file(buildings)
    gpd.GeoDataFrame({"name": ["a", "b"]},
                     geometry=[Point(1, 1), Point(4, 4)],
                     crs="EPSG:25832").to_file(points)
    # Was native:intersection wirklich schreibt: die Attribute beider Ebenen,
    # die Geometrie der Overlay-Ebene.
    gpd.GeoDataFrame({"building": ["yes"] * 2, "name": ["a", "b"]},
                     geometry=[Point(1, 1), Point(4, 4)],
                     crs="EPSG:25832").to_file(out)

    params = {"INPUT": str(buildings), "OVERLAY": str(points), "OUTPUT": str(out)}
    warning = _swapped_geometry_warning(params, {"results": {"OUTPUT": str(out)}},
                                        "native:intersection")
    assert warning and "OVERLAY's" in warning
    assert "qgis_extract_by_location" in warning, "die Warnung muss den Ausweg nennen"


def test_intersection_stays_quiet_when_the_family_survives(tmp_path):
    """Polygon ∩ Polygon = Polygon — der Normalfall, und er darf nichts melden."""
    from chester.capabilities.qgis import _swapped_geometry_warning

    a, b, out = tmp_path / "a.gpkg", tmp_path / "b.gpkg", tmp_path / "o.gpkg"
    for path, geom in ((a, box(0, 0, 4, 4)), (b, box(2, 2, 6, 6)),
                       (out, box(2, 2, 4, 4))):
        gpd.GeoDataFrame({"x": [1]}, geometry=[geom], crs="EPSG:25832").to_file(path)

    assert _swapped_geometry_warning(
        {"INPUT": str(a), "OVERLAY": str(b), "OUTPUT": str(out)},
        {"results": {"OUTPUT": str(out)}}, "native:intersection") is None


def test_only_intersection_is_examined(tmp_path):
    """`native:clip` erbt den Typ des Inputs — dort wäre die Prüfung nur Lärm."""
    from chester.capabilities.qgis import _swapped_geometry_warning

    a, b, out = tmp_path / "a.gpkg", tmp_path / "b.gpkg", tmp_path / "o.gpkg"
    gpd.GeoDataFrame({"x": [1]}, geometry=[box(0, 0, 4, 4)],
                     crs="EPSG:25832").to_file(a)
    gpd.GeoDataFrame({"x": [1]}, geometry=[Point(1, 1)], crs="EPSG:25832").to_file(b)
    gpd.GeoDataFrame({"x": [1]}, geometry=[Point(1, 1)], crs="EPSG:25832").to_file(out)
    assert _swapped_geometry_warning(
        {"INPUT": str(a), "OVERLAY": str(b), "OUTPUT": str(out)},
        {"results": {"OUTPUT": str(out)}}, "native:clip") is None


def test_a_join_that_matched_nothing_is_not_reported_as_success():
    """`JOINED_COUNT: 0` ist kein Erfolg — der stille Fall, den nur die Probe fing.

    Gemessen 2026-09-05 (`join-leading-zero-ags`): `native:joinattributestable` gab
    `{"JOINED_COUNT": 0, "UNJOINABLE_COUNT": 4}` zurück, `qgis_run` reichte das als
    `ok: true` weiter. Die Ausgabedatei existierte, trug alle vier Gemeindepolygone
    und die angehängte Spalte — in jeder Zeile leer. Beim Öffnen sieht ein solches
    Artefakt völlig normal aus.
    """
    from chester.capabilities.qgis import _did_nothing_warning

    why = _did_nothing_warning(
        {"results": {"JOINED_COUNT": 0, "UNJOINABLE_COUNT": 4, "OUTPUT": "x.gpkg"}}
    )
    assert why is not None
    assert "0 of 4" in why and "NULL" in why
    assert "vector_info" in why, "die Warnung muss den nächsten Griff nennen"


def test_a_join_that_worked_draws_no_warning():
    """Kein Lärm auf dem Normalfall — sonst lernt das Modell, das Feld zu überlesen."""
    from chester.capabilities.qgis import _did_nothing_warning

    assert _did_nothing_warning(
        {"results": {"JOINED_COUNT": 4, "UNJOINABLE_COUNT": 0, "OUTPUT": "x.gpkg"}}
    ) is None
    # Nichts zu verbinden ist auch nichts zu melden: leere Eingabe, kein Rest.
    assert _did_nothing_warning(
        {"results": {"JOINED_COUNT": 0, "UNJOINABLE_COUNT": 0}}
    ) is None
    # Und ein Verfahren ohne diese Zähler bleibt unberührt.
    assert _did_nothing_warning({"results": {"OUTPUT": "x.gpkg"}}) is None


def _layer(path, geoms):
    gpd.GeoDataFrame({"x": list(range(len(geoms)))}, geometry=geoms,
                     crs="EPSG:25832").to_file(path)
    return str(path)


def test_an_empty_result_from_a_full_input_is_not_a_success(tmp_path):
    """Voll rein, leer raus, `ok: true` — der teuerste stille Erfolg des Tages.

    Gemessen 2026-09-05 (`supermarket-accessibility-choropleth`): `native:clip`
    machte aus 127 Gemeindepolygonen **0**, danach zählte
    `native:countpointsinpolygon` in die leere Ebene und `native:intersection`
    schnitt sie erneut — drei Erfolgsmeldungen über nichts, jede mit einer gültigen,
    leeren GeoPackage-Datei. Keine der bestehenden Prüfungen konnte das sehen.
    """
    from chester.capabilities.qgis import _empty_result_warning

    src = _layer(tmp_path / "in.gpkg", [box(0, 0, 4, 4), box(5, 5, 9, 9)])
    out = tmp_path / "out.gpkg"
    gpd.GeoDataFrame({"x": []}, geometry=[], crs="EPSG:25832").to_file(out)
    why = _empty_result_warning({"INPUT": src, "OUTPUT": str(out)},
                                {"results": {"OUTPUT": str(out)}}, "native:clip")
    assert why is not None
    assert "2 feature(s) went in, 0 came out" in why
    assert "vector_info" in why and "fixgeometries" in why


def test_an_empty_input_draws_no_warning(tmp_path):
    """Leer rein, leer raus — dann ist nichts die ehrliche Antwort."""
    from chester.capabilities.qgis import _empty_result_warning

    empty = tmp_path / "in.gpkg"
    gpd.GeoDataFrame({"x": []}, geometry=[], crs="EPSG:25832").to_file(empty)
    out = tmp_path / "out.gpkg"
    gpd.GeoDataFrame({"x": []}, geometry=[], crs="EPSG:25832").to_file(out)
    assert _empty_result_warning({"INPUT": str(empty), "OUTPUT": str(out)},
                                 {"results": {"OUTPUT": str(out)}}, "native:clip") is None


def test_a_raster_output_is_not_mistaken_for_an_empty_layer(tmp_path):
    """`{}` ist eine leere Vektorebene, `None` ist „kein lesbarer Vektor".

    Ohne diese Unterscheidung würde jedes Verfahren, das aus Vektoren ein Raster
    macht (`native:rasterize`), sich selbst des Nichtstuns bezichtigen.
    """
    from chester.capabilities.qgis import _empty_result_warning

    src = _layer(tmp_path / "in.gpkg", [box(0, 0, 4, 4)])
    ras = tmp_path / "out.tif"
    ras.write_bytes(b"II*\x00not-a-vector")
    assert _empty_result_warning({"INPUT": src, "OUTPUT": str(ras)},
                                 {"results": {"OUTPUT": str(ras)}}, "native:rasterize") is None


def test_the_check_only_sees_INPUT_and_LAYERS(tmp_path):
    """Bekannte Lücke, festgehalten statt stillschweigend gelassen.

    `_primary_input` liest nur `INPUT`/`LAYERS`. `native:countpointsinpolygon` hat
    weder — sein Eingang heißt `POLYGONS` —, also bleibt der Schritt ungeprüft. Im
    Lauf vom 2026-09-05 war das folgenlos, weil der Clip davor bereits gewarnt hatte;
    die Kette warnt an ihrer ersten Bruchstelle, nicht an jeder.
    """
    from chester.capabilities.qgis import _empty_result_warning

    src = _layer(tmp_path / "pts.gpkg", [Point(1, 1)])
    out = tmp_path / "out.gpkg"
    gpd.GeoDataFrame({"x": []}, geometry=[], crs="EPSG:25832").to_file(out)
    assert _empty_result_warning({"POLYGONS": src, "OUTPUT": str(out)},
                                 {"results": {"OUTPUT": str(out)}},
                                 "native:countpointsinpolygon") is None


def _declared(path, geoms, declare):
    """Eine Ebene, deren Kopf `declare` behauptet — geschrieben wie QGIS es tut."""
    gpd.GeoDataFrame({"x": list(range(len(geoms)))}, geometry=geoms,
                     crs="EPSG:25832").to_file(path, geometry_type=declare)
    return str(path)


def test_a_header_that_lies_about_its_contents_is_named(tmp_path):
    """Der Kopf sagt Punkt, die Datei enthält Polygone — die Ursache hinter zwei
    der langlebigsten stillen Fehlschläge dieses Projekts.

    Gefunden 2026-09-05 durch Halbieren von `supermarket-accessibility-choropleth`:
    `native:extractbyexpression` filterte korrekt auf 127 Polygone, übernahm aber die
    Deklaration der gemischten Quelle — `POINT`, wegen zweier Punkte unter 319
    Objekten. QGIS meldet für diese Datei `wkbType() == 1`, pyogrio liest 117 Polygon
    + 10 MultiPolygon heraus. Jeder Folgeschritt glaubt dem Kopf, richtet seine
    Ausgabe auf Punkte, trifft nichts und schreibt eine gültige leere Datei mit
    `ok: true`. Dieselben 127 Objekte mit richtiger Deklaration neu geschrieben:
    derselbe Clip liefert 58.
    """
    from chester.capabilities.qgis import _type_declaration_warning

    bad = _declared(tmp_path / "mistyped.gpkg", [box(0, 0, 4, 4), box(5, 5, 9, 9)], "Point")
    why = _type_declaration_warning({"INPUT": bad, "OUTPUT": str(tmp_path / "o.gpkg")},
                                    {"results": {}}, "native:clip")
    assert why is not None
    assert "DECLARES `Point`" in why and "2× Polygon" in why
    assert "EMPTY file while reporting success" in why
    assert "vector_info" in why


def test_an_honest_header_draws_no_warning(tmp_path):
    """Und eine Ebene, deren Kopf stimmt, bleibt unbehelligt — auch als MultiPolygon
    über einfachen Polygonen, denn das ist dieselbe Familie."""
    from chester.capabilities.qgis import _type_declaration_warning

    ok = _declared(tmp_path / "fine.gpkg", [box(0, 0, 4, 4)], "MultiPolygon")
    assert _type_declaration_warning({"INPUT": ok}, {"results": {}}, "native:clip") is None


def test_a_mistyped_output_warns_the_next_step(tmp_path):
    """Eine falsch deklarierte **Ausgabe** ist die Falle für den nächsten Aufruf."""
    from chester.capabilities.qgis import _type_declaration_warning

    good = _declared(tmp_path / "in.gpkg", [box(0, 0, 4, 4)], "Polygon")
    bad = _declared(tmp_path / "out.gpkg", [box(1, 1, 2, 2)], "Point")
    why = _type_declaration_warning({"INPUT": good, "OUTPUT": bad},
                                    {"results": {"OUTPUT": bad}}, "native:clip")
    assert why is not None and "this step just wrote" in why


def test_an_honestly_declared_mixed_layer_is_fine(tmp_path):
    """Gemischt UND richtig deklariert gibt es — dafür ist `GEOMETRY` da.

    Die GeoPackage-Spezifikation kennt den Obertyp: „in dieser Tabelle darf jede
    Geometrieart vorkommen". OGR nennt ihn `wkbUnknown`, pyogrio `"Unknown"` — der
    Name liest sich wie ein Defekt und ist die ehrliche Angabe. Genau so schreibt
    `osm_features` eine Ebene aus Ladenpunkten und Ladengebäuden, und genau so
    schreibt geopandas einen gemischten Rahmen.
    """
    from chester.capabilities.qgis import _mistyped_layer

    honest = tmp_path / "mixed.gpkg"
    gpd.GeoDataFrame({"x": [1, 2]}, geometry=[Point(1, 1), box(0, 0, 4, 4)],
                     crs="EPSG:25832").to_file(honest)
    assert _mistyped_layer(str(honest)) is None


def test_a_mixed_layer_declared_as_one_type_is_the_worst_case(tmp_path):
    """Gemischt und als **ein** Typ deklariert — dort entsteht die Vergiftung.

    Gemessen 2026-09-05: `osm_features` schrieb `supermarkets.gpkg` korrekt als
    `GEOMETRY` (138 Polygone, 108 Punkte). `native:reprojectlayer` machte daraus
    `supermarkets_proj.gpkg` mit dem Kopf `POINT` — bei unverändertem Inhalt, 246
    rein, 246 raus. GDAL merkt das beim Schreiben ausdrücklich an („not normally
    allowed by the GeoPackage specification, but the driver will however do it") und
    schreibt trotzdem. Eine frühere Fassung dieser Prüfung nahm jede gemischte Ebene
    aus und verfehlte damit genau diesen Schritt.
    """
    from chester.capabilities.qgis import _mistyped_layer

    bad = _declared(tmp_path / "poisoned.gpkg", [Point(1, 1), box(0, 0, 4, 4)], "Point")
    found = _mistyped_layer(bad)
    assert found is not None
    assert found[0] == "Point" and found[1] == {"Point": 1, "Polygon": 1}
