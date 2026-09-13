"""Die elf Vektoroperationen auf GeoPandas (`chester/geoops.py`) — offline, ohne QGIS.

Phase KQ Schritt 2. Geprueft wird nicht, dass geopandas rechnen kann, sondern dass die
Fallen kodiert sind, die dieses Projekt bezahlt hat: metrische Arbeit in einem
geographischen CRS, leeres Ergebnis aus nicht-leerer Eingabe, und der Unterschied
zwischen Auswaehlen und Schneiden.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Point, box

from chester import geoops


def _ws(tmp_path):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    return str(tmp_path)


def _layer(tmp_path, name, geoms, crs="EPSG:25832", **cols):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    data = cols or {"x": list(range(len(geoms)))}
    gpd.GeoDataFrame(data, geometry=geoms, crs=crs).to_file(
        tmp_path / "geocache" / f"{name}.gpkg"
    )
    return f"{name}.gpkg"


def test_all_eleven_operations_are_exported():
    """Ein Verzeichnis, damit Werkzeuge und Sandbox nicht auseinanderlaufen."""
    assert set(geoops.OPERATIONS) == {
        "reproject", "buffer", "clip", "intersection", "extract_by_location",
        "extract_by_attribute", "dissolve", "merge", "join", "add_field", "field_sum",
    }


def test_every_operation_is_also_a_tool(tmp_path):
    """Die Kopplung selbst, nicht nur die Liste.

    Der Befund vom 2026-09-07: Was nur im Sandbox-Namensraum steht, benutzt das Modell
    nicht. Also darf keine Operation dort landen, ohne im Werkzeugkatalog zu stehen —
    dieser Test schlägt fehl, sobald jemand `geoops` erweitert und `vectorops`
    vergisst.
    """
    from chester.capabilities.vectorops import op_tools

    tools = {t.__name__ for t in op_tools(str(tmp_path))}
    assert tools == {f"vector_{name}" for name in geoops.OPERATIONS}


def test_buffer_refuses_a_geographic_crs(tmp_path):
    """500 in Grad sind rund 55.000 km — das darf kein Ergebnis werden.

    Die aelteste Falle der Bank (`buffer-in-degrees`). Ein Puffer in WGS84 liefert
    eine Form, die plausibel aussieht und um Groessenordnungen falsch ist; eine
    Absage ist die einzige ehrliche Antwort.
    """
    src = _layer(tmp_path, "pts", [Point(12.09, 49.01)], crs="EPSG:4326")
    res = geoops.buffer(src, "out.gpkg", 500, workspace=_ws(tmp_path))
    assert res["ok"] is False
    assert "geographic CRS" in res["error"] and "DEGREES" in res["error"]
    assert "25832" in res["error"], "die Absage muss den Ausweg nennen"


def test_clip_keeps_every_geometry_type(tmp_path):
    """Der Unterschied zu `native:clip`, in einem Test.

    Gemessen 2026-09-05: QGIS' Clip schreibt EINEN Geometrietyp — den, den der
    Dateikopf deklariert — und liess von 246 Supermaerkten 18 uebrig, weil die 138
    Polygone wortlos wegfielen. Ueber geopandas bleiben beide Familien erhalten.
    """
    src = _layer(tmp_path, "mixed", [Point(1, 1), box(0, 0, 4, 4), Point(100, 100)])
    mask = _layer(tmp_path, "mask", [box(-1, -1, 10, 10)])
    res = geoops.clip(src, mask, "clipped.gpkg", workspace=_ws(tmp_path))
    assert res["ok"] is True and res["features_out"] == 2
    got = gpd.read_file(tmp_path / "geocache" / "clipped.gpkg")
    assert set(got.geom_type) == {"Point", "Polygon"}, "ein Typ ging verloren"


def test_an_empty_result_says_so(tmp_path):
    """Voll rein, leer raus — der teuerste stille Erfolg des Projekts."""
    src = _layer(tmp_path, "here", [box(0, 0, 4, 4)])
    far = _layer(tmp_path, "far", [box(1000, 1000, 1004, 1004)])
    res = geoops.clip(src, far, "empty.gpkg", workspace=_ws(tmp_path))
    assert res["ok"] is True and res["features_out"] == 0
    assert "output is EMPTY" in res["warning"]
    assert "do not build on it" in res["warning"]


def test_extract_by_location_keeps_whole_features(tmp_path):
    """Auswaehlen ist nicht Schneiden — die Probe `intersection-not-selection`.

    Ein Polygon, das die Maske nur beruehrt, kommt **ganz** durch; `clip` haette es
    an der Kante abgeschnitten. Die beiden zu verwechseln ergibt Flaechen, die um ein
    Vielfaches danebenliegen.
    """
    src = _layer(tmp_path, "polys", [box(0, 0, 10, 10)])
    mask = _layer(tmp_path, "small", [box(0, 0, 2, 2)])
    ws = _ws(tmp_path)
    sel = geoops.extract_by_location(src, mask, "sel.gpkg", workspace=ws)
    cut = geoops.clip(src, mask, "cut.gpkg", workspace=ws)
    a_sel = gpd.read_file(tmp_path / "geocache" / "sel.gpkg").geometry.area.sum()
    a_cut = gpd.read_file(tmp_path / "geocache" / "cut.gpkg").geometry.area.sum()
    assert sel["features_out"] == cut["features_out"] == 1
    assert a_sel == 100 and a_cut == 4, "Auswahl behaelt 100 m², Schnitt behaelt 4 m²"


def test_area_needs_a_metric_crs(tmp_path):
    """Quadratgrade sind keine Quadratmeter — in beiden Werkzeugen geprueft."""
    src = _layer(tmp_path, "geo", [box(12.0, 49.0, 12.1, 49.1)], crs="EPSG:4326")
    ws = _ws(tmp_path)
    summed = geoops.field_sum(src, "area", workspace=ws)
    added = geoops.add_field(src, "out.gpkg", "a", "area", workspace=ws)
    assert summed["ok"] is False and "geographic CRS" in summed["error"]
    assert added["ok"] is False and "square degrees" in added["error"]


def test_field_sum_answers_with_the_number(tmp_path):
    """Eine Summe ist eine Antwort, keine Datei — und sie nennt ihr CRS dazu."""
    src = _layer(tmp_path, "sq", [box(0, 0, 10, 10), box(20, 20, 30, 30)])
    res = geoops.field_sum(src, "area", workspace=_ws(tmp_path))
    assert res["ok"] is True and res["sum"] == 200.0
    assert res["features"] == 2 and res["crs"] == "EPSG:25832"


def test_a_non_numeric_column_is_refused(tmp_path):
    """Eine Summe ueber Text hat keine Bedeutung — das gehoert gesagt, nicht geraten."""
    src = _layer(tmp_path, "named", [Point(1, 1), Point(2, 2)], label=["a", "b"])
    res = geoops.field_sum(src, "label", workspace=_ws(tmp_path))
    assert res["ok"] is False and "not numeric" in res["error"]


def test_every_written_output_carries_provenance(tmp_path):
    """Jede schreibende Operation stempelt ihren Sidecar — die Regel des Projekts."""
    import os

    src = _layer(tmp_path, "src", [box(0, 0, 4, 4)])
    res = geoops.reproject(src, "wgs.gpkg", "EPSG:4326", workspace=_ws(tmp_path))
    assert res["ok"] is True
    assert os.path.isfile(res["output"] + ".meta.json")


def test_a_mistyped_layer_name_is_a_refusal_not_a_crash(tmp_path):
    """Gemessen 2026-09-07 (`supermarket-accessibility-choropleth`): ein Wort daneben.

    Das Modell schrieb `regress_supermarkets_split_polygon.gpkg` statt
    `regensburg_…`. `gpd.read_file` warf `DataSourceError`, die Ausnahme verliess das
    Werkzeug und **beendete den Lauf** nach 930 s — bei einem Tippfehler. Alle elf
    Operationen teilten diesen Weg.
    """
    have = _layer(tmp_path, "regensburg_supermarkets", [Point(1, 1)])
    for call in (
        lambda: geoops.reproject("regress_supermarkets.gpkg", "o.gpkg", "EPSG:4326",
                                 workspace=_ws(tmp_path)),
        lambda: geoops.buffer("regress_supermarkets.gpkg", "o.gpkg", 10,
                              workspace=_ws(tmp_path)),
        lambda: geoops.clip(have, "regress_supermarkets.gpkg", "o.gpkg",
                            workspace=_ws(tmp_path)),
        lambda: geoops.merge([have, "regress_supermarkets.gpkg"], "o.gpkg",
                             workspace=_ws(tmp_path)),
    ):
        res = call()
        assert res["ok"] is False, "eine Absage, kein Absturz"
        assert "nothing was read and nothing written" in res["error"]
        assert have in res["did_you_mean"], "und der Rückgabekanal nennt den Nachbarn"


def test_the_hint_falls_back_to_what_is_there(tmp_path):
    """Ohne ähnlichen Namen: lieber den Bestand zeigen als gar nichts."""
    _layer(tmp_path, "gruenflaechen", [Point(1, 1)])
    res = geoops.buffer("voellig_anderes.gpkg", "o.gpkg", 10, workspace=_ws(tmp_path))
    assert res["ok"] is False
    assert "gruenflaechen.gpkg" in res["did_you_mean"]
