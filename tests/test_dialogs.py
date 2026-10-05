"""Evaluating the Test-Level-4 dialogues — without a model, without a network (Test-Level 1).

The dialogue runner is expensive (two live steps per case). The checking logic must not
be: it lives in `chester/dialogs.py` and is run here against invented steps.

The case category D8 comes from (2026-08-27): "markiere diese vier Adressen" got a 266 MB
GeoTIFF of nothing but zeros, the user reported "das Bild ist eine schwarze Fläche", and
the answer began with a guess at the cause **before anything had been measured**. That
is exactly what `tool_touched` checks.
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
    """The sharpest check of the first dialogue — and the one the real run broke."""
    measured = [_turn(tools=[("vector_info", {"path": "marked.tif"})])]
    ok, why = check({"turn": 1, "kind": "tool_touched", "contains": ".tif"},
                    measured, workspace=tmp_path)
    assert ok and ".tif" in why

    guessed = [_turn(tools=[("render_map", {"layers": ["a.gpkg"]})])]
    ok, why = check({"turn": 1, "kind": "tool_touched", "contains": ".tif"},
                    guessed, workspace=tmp_path)
    assert not ok and "gemessen wurde es nicht" in why


def test_tool_touched_measures_the_street_not_its_spelling(tmp_path):
    """A street name has several right spellings — the check must not hang on the chosen
    one.

    `street-buildings-then-refine` checks `tool_touched: "Regensburger Straße"`. Which form
    arrives there is up to OSM and the model: "Regensburger Str.", "Regensburgerstraße",
    lower case. Compared literally the run fails although the agent fetched exactly the
    street asked for — a false verdict that says nothing about the agent, the same kind
    as a criterion left standing after a change of place.
    """
    for written in ("Regensburger Straße", "Regensburger Str.", "Regensburgerstrasse",
                    "regensburger strasse"):
        turns = [_turn(tools=[("osm_features", {"where": {"addr:street": written}})])]
        ok, why = check({"turn": 1, "kind": "tool_touched",
                         "contains": "Regensburger Straße"}, turns, workspace=tmp_path)
        assert ok, f"{written!r} sollte treffen — {why}"


def test_tool_touched_still_says_no_to_a_different_street(tmp_path):
    """The relaxation must not wave everything through: the neighbouring town stays a fail."""
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
    """A step that leaves an empty raster has repaired nothing."""
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
    """The dialogue file itself: steps, criteria and only known check kinds."""
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
    """An aborted step leaves no session behind — after that the agent talks to a
    stranger.

    Observed on 2026-09-01: step 1 broke the 900 s limit, and "Gib die Karte als GeoTiff
    aus" got *"Da dies unser erster Austausch ist …"* ("since this is our first
    exchange"). Four of seven checks were green anyway, because the second step did
    nothing — `fewer_calls_than: 0 gegen 28` was the clearest of them.
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
    assert "abgebrochen" in lines[0]                       # the reason comes first
    assert "nicht gefahren" in lines[2]                    # no acquittal for step 2


def test_a_complete_dialogue_carries_no_abort_line(tmp_path):
    from chester.dialogs import aborted_after

    turns = [_turn(tools=[("geocode", {})]), _turn(tools=[("render_map", {})])]
    assert aborted_after(turns) is None
    passed, lines = evaluate({"checks": [{"turn": 2, "kind": "tool_called",
                                          "tool": "render_map"}]},
                             turns, workspace=tmp_path)
    assert passed and len(lines) == 1


def test_map_shows_family_reads_the_drawn_layer(tmp_path):
    """The check that was missing on 2026-09-01: circles instead of footprints.

    Seven of seven checks were green while the map showed four points —
    `native:intersection` had intersected the buildings with the geocoded addresses
    (polygon ∩ point = point), and the attributes came along, so every return looked
    right.

    What is asked is the **drawn** layer: the right polygons were on disk the whole run,
    just not on the map.
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


# ── validate: the editor guard ─────────────────────────────────────────


def test_the_bank_dialogs_are_valid():
    """The check must let the real cases through — otherwise it is too strict."""
    import json
    import pathlib

    from chester.dialogs import validate

    for line in pathlib.Path("agent-dialog-tests.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            case = json.loads(line)
            assert validate(case) == [], f"{case['id']}: {validate(case)}"


def test_an_unknown_check_kind_is_caught():
    """Otherwise the case fails only in the run — after twenty minutes of agent time."""
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
    """`fewer_calls_than` against the same step is always wrong and looks right while
    typing."""
    from chester.dialogs import validate

    problems = validate({"id": "x", "turns": [{"prompt_de": "a"}, {"prompt_de": "b"}],
                         "checks": [{"kind": "fewer_calls_than", "turn": 2, "than_turn": 2}]})
    assert any("mit sich selbst" in p for p in problems)


def test_every_kind_has_an_entry_in_required_args():
    """Whoever adds a check kind must say which field it needs — otherwise the editor
    lets it through unchecked."""
    from chester.dialogs import KINDS, REQUIRED_ARGS

    assert set(REQUIRED_ARGS) == set(KINDS)


def test_map_shows_family_finds_the_record_under_either_root(tmp_path):
    """The check kind ran for the first time on 2026-09-05 — and failed on the path.

    `probe.workspace()`, which `dialog.py` and the test app pass through, already returns
    the **geocache** directory; `_map_family` appended `geocache` a second time and
    searched in `…/geocache/geocache/`. `render_map` had reported `ok: true` twice, the
    file was there, and the check reported "no map drawn". In all archived runs before,
    this check kind occurs zero times — it was written and never executed.
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

    # (a) the caller passes the geocache directory — the real case
    gc = tmp_path / "a" / "geocache"
    _layer(gc / "x.gpkg")
    assert _map_family("polygon", gc)[0]

    # (b) Aufrufer reicht die Workspace-Wurzel — die ursprüngliche Annahme
    gc2 = tmp_path / "b" / "geocache"
    _layer(gc2 / "x.gpkg")
    assert _map_family("polygon", tmp_path / "b")[0]

    # (c) counter-check: without a map it stays a fail
    ok, why = _map_family("polygon", tmp_path / "leer")
    assert not ok and "keine Karte" in why
