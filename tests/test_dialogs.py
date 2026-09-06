"""Die Auswertung der Test-Level-4-Dialoge — ohne Modell, ohne Netz (Test-Level 1).

Der Dialog-Runner ist teuer (zwei Live-Schritte je Fall). Die Prüflogik darf das nicht
sein: Sie sitzt in `chester/dialogs.py` und wird hier gegen erfundene Schritte gefahren.

Der Fall, aus dem die Kategorie D8 stammt (2026-08-27): Auf „markiere diese vier
Adressen" kam ein 266-MB-GeoTIFF aus lauter Nullen, der Nutzer meldete „das Bild ist
eine schwarze Fläche", und die Antwort begann mit einer Ursachenvermutung, **bevor
irgendetwas gemessen war**. Genau das prüft `tool_touched`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chester.dialogs import KINDS, Turn, check, evaluate


def _turn(prompt="…", tools=(), answer="", written=()):
    t = Turn(prompt)
    t.tool_calls = list(tools)
    t.answer = answer
    t.written = list(written)
    return t


def test_tool_called_and_not_called(tmp_path):
    turns = [_turn(tools=[("geocode", {"query": "Domplatz 7"})])]
    assert check({"turn": 1, "kind": "tool_called", "tool": "geocode"},
                 turns, workspace=tmp_path)[0]
    ok, why = check({"turn": 1, "kind": "tool_not_called", "tool": "geocode"},
                    turns, workspace=tmp_path)
    assert not ok and "1× geocode" in why


def test_tool_touched_is_the_measured_before_explained_check(tmp_path):
    """Die schärfste Prüfung des ersten Dialogs — und die, die der echte Lauf riss."""
    measured = [_turn(tools=[("vector_info", {"path": "marked.tif"})])]
    ok, why = check({"turn": 1, "kind": "tool_touched", "contains": ".tif"},
                    measured, workspace=tmp_path)
    assert ok and ".tif" in why

    guessed = [_turn(tools=[("render_map", {"layers": ["a.gpkg"]})])]
    ok, why = check({"turn": 1, "kind": "tool_touched", "contains": ".tif"},
                    guessed, workspace=tmp_path)
    assert not ok and "gemessen wurde es nicht" in why


def test_tool_touched_measures_the_street_not_its_spelling(tmp_path):
    """Ein Straßenname hat mehrere richtige Schreibweisen — die Prüfung darf nicht
    an der gewählten hängen.

    `street-buildings-then-refine` prüft `tool_touched: "Regensburger Straße"`. Welche
    Form dort ankommt, entscheiden OSM und das Modell: „Regensburger Str.",
    „Regensburgerstraße", klein geschrieben. Wörtlich verglichen fällt der Lauf durch,
    obwohl der Agent exakt die verlangte Straße geholt hat — ein Fehlurteil, das nichts
    über den Agenten aussagt, derselbe Typ wie ein nach einem Ortswechsel
    stehengebliebenes Kriterium.
    """
    for written in ("Regensburger Straße", "Regensburger Str.", "Regensburgerstrasse",
                    "regensburger strasse"):
        turns = [_turn(tools=[("osm_features", {"where": {"addr:street": written}})])]
        ok, why = check({"turn": 1, "kind": "tool_touched",
                         "contains": "Regensburger Straße"}, turns, workspace=tmp_path)
        assert ok, f"{written!r} sollte treffen — {why}"


def test_tool_touched_still_says_no_to_a_different_street(tmp_path):
    """Die Lockerung darf nicht alles durchwinken: der Nachbarort bleibt ein Fehlschlag."""
    turns = [_turn(tools=[("osm_features", {"where": {"addr:street": "Hollerweg"}})])]
    ok, why = check({"turn": 1, "kind": "tool_touched", "contains": "Regensburger Straße"},
                    turns, workspace=tmp_path)
    assert not ok and "in keiner Schreibweise" in why


def test_answer_omits_the_broken_artifact(tmp_path):
    turns = [_turn(answer="Die Karte liegt in orange_buildings_excerpt.tif.")]
    ok, why = check({"turn": 1, "kind": "answer_omits", "contains": "orange_buildings_excerpt"},
                    turns, workspace=tmp_path)
    assert not ok and "steht in der Antwort" in why


def test_fewer_calls_than_compares_two_turns(tmp_path):
    turns = [_turn(tools=[("a", {})] * 10), _turn(tools=[("b", {})] * 3)]
    assert check({"turn": 2, "kind": "fewer_calls_than", "than_turn": 1},
                 turns, workspace=tmp_path)[0]
    ok, why = check({"turn": 1, "kind": "fewer_calls_than", "than_turn": 2},
                    turns, workspace=tmp_path)
    assert not ok and "10 gegen 3" in why


def test_no_flat_raster_reads_what_the_turn_wrote(tmp_path):
    """Ein Schritt, der ein leeres Raster hinterlässt, hat nichts repariert."""
    pytest.importorskip("rasterio")
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    black = tmp_path / "black.tif"
    with rasterio.open(black, "w", driver="GTiff", height=8, width=8, count=1,
                       dtype="float32", crs="EPSG:25832", nodata=-9999.0,
                       transform=from_origin(0, 8, 1, 1)) as dst:
        dst.write(np.zeros((8, 8), dtype="float32"), 1)

    turns = [_turn(written=[str(black)])]
    ok, why = check({"turn": 1, "kind": "no_flat_raster"}, turns, workspace=tmp_path)
    assert not ok and "every pixel is 0" in why


def test_a_turn_that_does_not_exist_fails_loudly(tmp_path):
    ok, why = check({"turn": 3, "kind": "tool_called", "tool": "x"},
                    [_turn()], workspace=tmp_path)
    assert not ok and "gibt es nicht" in why


def test_unknown_kinds_fail_loudly(tmp_path):
    ok, why = check({"turn": 1, "kind": "stimmung"}, [_turn()], workspace=tmp_path)
    assert not ok and "unbekannte Prüfart" in why


def test_evaluate_needs_every_check(tmp_path):
    dialog = {"checks": [
        {"turn": 1, "kind": "tool_called", "tool": "geocode"},
        {"turn": 1, "kind": "tool_called", "tool": "render_map"},
    ]}
    turns = [_turn(tools=[("geocode", {})])]
    passed, lines = evaluate(dialog, turns, workspace=tmp_path)
    assert not passed
    assert lines[0].startswith("  ✓") and lines[1].startswith("  ✗")


def test_the_shipped_dialog_is_well_formed():
    """Die Dialogdatei selbst: Schritte, Kriterien und nur bekannte Prüfarten."""
    rows = [json.loads(line) for line in
            Path("agent-dialog-tests.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert rows, "ohne Dialog misst Test-Level 4 nichts"
    for d in rows:
        assert d["id"] and d["category"] and d["origin"]
        assert len(d["turns"]) >= 2, "ein Dialog mit einem Schritt ist ein Prompt"
        for t in d["turns"]:
            assert t["prompt_de"] and t["criteria"]
        for c in d.get("checks", []):
            assert c["kind"] in KINDS and c["turn"] >= 1


def _timed_out_turn(**kw):
    t = _turn(**kw)
    t.timed_out = True
    return t


def test_a_dialogue_stops_after_a_capped_step(tmp_path):
    """Ein abgebrochener Schritt hinterlässt keine Sitzung — danach redet der Agent
    mit einem Fremden.

    Beobachtet am 2026-09-01: Schritt 1 riss den 900-s-Deckel, und auf „Gib die Karte
    als GeoTiff aus" kam *„Da dies unser erster Austausch ist …"*. Vier von sieben
    Prüfungen standen trotzdem auf grün, weil der zweite Schritt nichts tat —
    `fewer_calls_than: 0 gegen 28` war die deutlichste davon.
    """
    from chester.dialogs import aborted_after

    turns = [_timed_out_turn(tools=[("geocode", {})] * 28)]
    assert aborted_after(turns) == 1

    dialog = {"checks": [
        {"turn": 1, "kind": "tool_called", "tool": "geocode"},
        {"turn": 2, "kind": "fewer_calls_than", "than_turn": 1},
    ]}
    passed, lines = evaluate(dialog, turns, workspace=tmp_path)
    assert not passed
    assert "abgebrochen" in lines[0]                       # der Grund steht oben
    assert "nicht gefahren" in lines[2]                    # kein Freispruch für Schritt 2


def test_a_complete_dialogue_carries_no_abort_line(tmp_path):
    from chester.dialogs import aborted_after

    turns = [_turn(tools=[("geocode", {})]), _turn(tools=[("render_map", {})])]
    assert aborted_after(turns) is None
    passed, lines = evaluate({"checks": [{"turn": 2, "kind": "tool_called",
                                          "tool": "render_map"}]},
                             turns, workspace=tmp_path)
    assert passed and len(lines) == 1


def test_map_shows_family_reads_the_drawn_layer(tmp_path):
    """Die Prüfung, die am 2026-09-01 gefehlt hat: Kreise statt Grundflächen.

    Sieben von sieben Prüfungen standen auf grün, während die Karte vier Punkte
    zeigte — `native:intersection` hatte die Gebäude mit den geokodierten Adressen
    verschnitten (Polygon ∩ Punkt = Punkt), und die Attribute kamen mit, also sah
    jede Rückgabe richtig aus.

    Gefragt wird die **gezeichnete** Ebene: Die richtigen Polygone lagen in dem Lauf
    die ganze Zeit auf der Platte, nur nicht auf der Karte.
    """
    import geopandas as gpd
    from shapely.geometry import Point, box

    cache = tmp_path / "geocache"
    cache.mkdir()
    points, polys = cache / "marked.gpkg", cache / "footprints.gpkg"
    gpd.GeoDataFrame({"building": ["yes"]}, geometry=[Point(1, 1)],
                     crs="EPSG:25832").to_file(points)
    gpd.GeoDataFrame({"building": ["yes"]}, geometry=[box(0, 0, 2, 2)],
                     crs="EPSG:25832").to_file(polys)
    record = cache / "last_map.json"

    record.write_text(json.dumps({"layers": [str(points)]}), encoding="utf-8")
    ok, why = check({"turn": 1, "kind": "map_shows_family", "family": "polygon"},
                    [_turn()], workspace=tmp_path)
    assert not ok and "point" in why

    record.write_text(json.dumps({"layers": [str(polys)]}), encoding="utf-8")
    assert check({"turn": 1, "kind": "map_shows_family", "family": "polygon"},
                 [_turn()], workspace=tmp_path)[0]


def test_map_shows_family_without_a_map_fails(tmp_path):
    ok, why = check({"turn": 1, "kind": "map_shows_family", "family": "polygon"},
                    [_turn()], workspace=tmp_path)
    assert not ok and "keine Karte" in why


# ── validate: der Editor-Wächter ─────────────────────────────────────────


def test_the_bank_dialogs_are_valid():
    """Die Prüfung muss die echten Fälle durchlassen — sonst ist sie zu streng."""
    import json
    import pathlib

    from chester.dialogs import validate

    for line in pathlib.Path("agent-dialog-tests.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = json.loads(line)
            assert validate(case) == [], f"{case['id']}: {validate(case)}"


def test_an_unknown_check_kind_is_caught():
    """Sonst scheitert der Fall erst im Lauf — nach zwanzig Minuten Agentenzeit."""
    from chester.dialogs import validate

    problems = validate({"id": "x", "turns": [{"prompt_de": "hallo"}],
                         "checks": [{"kind": "quatsch", "turn": 1}]})
    assert any("quatsch" in p for p in problems)


def test_a_missing_argument_is_caught():
    from chester.dialogs import validate

    problems = validate({"id": "x", "turns": [{"prompt_de": "hallo"}],
                         "checks": [{"kind": "tool_called", "turn": 1}]})
    assert any("'tool'" in p for p in problems)


def test_a_turn_index_out_of_range_is_caught():
    from chester.dialogs import validate

    problems = validate({"id": "x", "turns": [{"prompt_de": "hallo"}],
                         "checks": [{"kind": "no_dead_path", "turn": 4}]})
    assert any("turn=4" in p for p in problems)


def test_comparing_a_turn_with_itself_is_caught():
    """`fewer_calls_than` gegen denselben Schritt ist immer falsch und sieht
    beim Tippen richtig aus."""
    from chester.dialogs import validate

    problems = validate({"id": "x", "turns": [{"prompt_de": "a"}, {"prompt_de": "b"}],
                         "checks": [{"kind": "fewer_calls_than", "turn": 2, "than_turn": 2}]})
    assert any("mit sich selbst" in p for p in problems)


def test_every_kind_has_an_entry_in_required_args():
    """Wer eine Prüfart hinzufügt, muss sagen, welches Feld sie braucht —
    sonst lässt der Editor sie ungeprüft durch."""
    from chester.dialogs import KINDS, REQUIRED_ARGS

    assert set(REQUIRED_ARGS) == set(KINDS)


def test_map_shows_family_finds_the_record_under_either_root(tmp_path):
    """Die Prüfart lief am 2026-09-05 zum ersten Mal — und scheiterte am Pfad.

    `probe.workspace()`, was `dialog.py` und die Test App durchreichen, liefert
    bereits das **geocache**-Verzeichnis; `_map_family` hängte `geocache` ein
    zweites Mal an und suchte in `…/geocache/geocache/`. `render_map` hatte zweimal
    `ok: true` gemeldet, die Datei lag da, und die Prüfung meldete „keine Karte
    gezeichnet". In allen archivierten Läufen davor kommt diese Prüfart null Mal
    vor — sie war geschrieben und nie ausgeführt.
    """
    import json

    import geopandas as gpd
    from shapely.geometry import box

    from chester.dialogs import _map_family

    def _layer(at):
        at.parent.mkdir(parents=True, exist_ok=True)
        gpd.GeoDataFrame({"a": [1]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:25832").to_file(at)
        (at.parent / "last_map.json").write_text(
            json.dumps({"layers": [str(at)]}), encoding="utf-8"
        )

    # (a) Aufrufer reicht das geocache-Verzeichnis — der reale Fall
    gc = tmp_path / "a" / "geocache"
    _layer(gc / "x.gpkg")
    assert _map_family("polygon", gc)[0]

    # (b) Aufrufer reicht die Workspace-Wurzel — die ursprüngliche Annahme
    gc2 = tmp_path / "b" / "geocache"
    _layer(gc2 / "x.gpkg")
    assert _map_family("polygon", tmp_path / "b")[0]

    # (c) Gegenprobe: ohne Karte bleibt es ein Fehlschlag
    ok, why = _map_family("polygon", tmp_path / "leer")
    assert not ok and "keine Karte" in why
