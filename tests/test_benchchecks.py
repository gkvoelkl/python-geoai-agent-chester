"""The bank's mechanical checks (Phase MB): each kind passes the right run and fails the wrong one.

Test-Level 1 — no model, Test-Level-2 fixtures. A run is built by hand as a `Turn` with the
tool results a real run leaves; the cases are the measured ones — 25 school points instead
of 84 schools (Nemotron, 2026-11-07), a buffer past the city limit (agent, 2026-11-07),
Copernicus instead of DGM1 and ~1020 m for the Arbersee (2026-10-26).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import geopandas as gpd
import pytest

from chester import benchchecks, dialogs

FIXTURES = Path(__file__).parent.parent / "probes" / "fixtures"
BANK = Path(__file__).parent.parent / "agent-test-prompts.jsonl"


def _turn(results: list[tuple[str, dict]], answer: str = "",
          calls: list[str] | None = None) -> dialogs.Turn:
    turn = dialogs.Turn("q")
    turn.tool_results = results
    turn.tool_calls = [(name, {}) for name in (calls or [n for n, _ in results])]
    turn.answer = answer
    return turn


@pytest.fixture
def ws(tmp_path) -> Path:
    for name in ("stops.gpkg", "district.gpkg", "schools_4326.gpkg"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    gpd.read_file(FIXTURES / "stops.gpkg").buffer(100).to_frame("geometry").to_file(
        tmp_path / "stops_buffer.gpkg")
    gpd.read_file(FIXTURES / "stops.gpkg").buffer(5000).to_frame("geometry").to_file(
        tmp_path / "huge_buffer.gpkg")
    return tmp_path


def _ok(path: Path) -> dict:
    return {"ok": True, "output": str(path)}


def test_crs_and_count_of_the_produced_layer(ws):
    turn = _turn([("vector_buffer", _ok(ws / "stops_buffer.gpkg"))])
    crs = {"kind": "artifact_crs", "tool": "vector_buffer", "crs": "EPSG:25832"}
    assert benchchecks.check_artifact(crs, turn, ws)[0]
    assert not benchchecks.check_artifact({**crs, "crs": "EPSG:4326"}, turn, ws)[0]
    count = {"kind": "artifact_count_in_range", "tool": "vector_buffer", "min": 4, "max": 4}
    assert benchchecks.check_artifact(count, turn, ws)[0]
    # 25 points where 84 schools are expected — the Nemotron case
    assert not benchchecks.check_artifact({**count, "min": 75, "max": 95}, turn, ws)[0]


def test_a_layer_reaching_past_the_boundary_is_caught(ws):
    inside = {"kind": "artifact_inside", "tool": "vector_buffer",
              "boundary_tool": "fetch_boundaries", "max_km2": 0.01}
    small = _turn([("fetch_boundaries", _ok(ws / "district.gpkg")),
                   ("vector_buffer", _ok(ws / "stops_buffer.gpkg"))])
    huge = _turn([("fetch_boundaries", _ok(ws / "district.gpkg")),
                  ("vector_buffer", _ok(ws / "huge_buffer.gpkg"))])
    assert not benchchecks.check_artifact(inside, huge, ws)[0]
    ok, why = benchchecks.check_artifact(inside, small, ws)
    assert "km² außerhalb" in why  # the measurement is reported either way
    assert ok == (float(why.split(": ")[1].split(" ")[0]) <= 0.01)


def test_no_successful_call_or_a_missing_file_fails_with_the_reason(ws):
    a = {"kind": "artifact_crs", "tool": "vector_buffer", "crs": "EPSG:25832"}
    assert benchchecks.check_artifact(a, _turn([]), ws) == (
        False, "kein erfolgreicher Aufruf von vector_buffer")
    failed = _turn([("vector_buffer", {"ok": False, "error": "x"})])
    assert not benchchecks.check_artifact(a, failed, ws)[0]
    gone = _turn([("vector_buffer", _ok(ws / "nowhere.gpkg"))])
    assert "fehlt" in benchchecks.check_artifact(a, gone, ws)[1]


def test_a_copied_workspace_is_found_by_the_geocache_tail(ws):
    old = "/old/.chester/workspace/geocache/stops_buffer.gpkg"
    turn = _turn([("vector_buffer", {"ok": True, "output": old})])
    a = {"kind": "artifact_crs", "tool": "vector_buffer", "crs": "EPSG:25832"}
    assert benchchecks.check_artifact(a, turn, ws)[0]


TABLE = ("| **Großer Arbersee** | 49.0985° N | 13.1552° O | 934,9 m |\n"
         "| **Großer Arber** | 49.1125° N | 13.1362° O | 1.452,7 m ü. NHN |")


def test_numbers_in_the_answer_are_read_by_label_and_unit():
    assert benchchecks.answer_number(TABLE, "Großer Arbersee", "m") == 934.9
    assert benchchecks.answer_number(TABLE, "Großer Arber", "m") == 1452.7  # not "…see"
    assert benchchecks.answer_number(TABLE, "Großer Arber") == 49.1125  # no unit: latitude
    assert benchchecks.answer_number("Der Arbersee liegt bei ~1020 m.", "Arbersee", "m") == 1020
    assert benchchecks.answer_number("nichts dazu", "Arbersee", "m") is None
    assert benchchecks.answer_number("Arber: 1.452 m", "Arber", "m") == 1452  # German thousands


def test_the_copernicus_answer_of_2026_10_26_fails():
    a = {"kind": "answer_number_in_range", "near": "Arbersee", "unit": "m",
         "min": 930, "max": 940}
    copernicus = _turn([], answer="| **Großer Arbersee** | 49.0985° N | 13.1552° O | ~1020 m |")
    assert not benchchecks.check_artifact(a, copernicus, Path("."))[0]
    assert benchchecks.check_artifact(a, _turn([], answer=TABLE), Path("."))[0]


def test_dialogue_kinds_run_on_the_single_turn(ws):
    rec = {"checks": [{"kind": "tool_called", "tool": "fetch_dgm1"},
                      {"kind": "tool_not_called", "tool": "fetch_dem"}]}
    good, _ = benchchecks.evaluate(rec, _turn([], calls=["geocode", "fetch_dgm1"]), ws)
    bad, lines = benchchecks.evaluate(rec, _turn([], calls=["geocode", "fetch_dem"]), ws)
    assert good and not bad
    assert lines[0].startswith("  ✗ tool_called")


def test_every_bank_record_with_checks_is_valid():
    rows = [json.loads(line) for line in BANK.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    with_checks = [r for r in rows if r.get("checks")]
    assert with_checks, "no record carries checks"
    for r in with_checks:
        assert benchchecks.validate(r) == [], r["id"]
    assert benchchecks.validate({"checks": [{"kind": "artifact_crs", "tool": "x"}]})


def test_the_report_after_a_run_never_raises(tmp_path):
    """Called by testprompt.py after the run — a broken check must not cost the run."""
    rec = {"checks": [{"kind": "tool_called", "tool": "osm_features"}]}
    text = benchchecks.run_report(rec, "no-such-session", tmp_path, tmp_path)
    assert text.startswith("\n[checks] nicht ausgewertet") and "keine Sitzung" in text


def test_the_crs_of_a_raster_output_is_read_too(ws):
    """`fetch_dop` and `slope` write rasters — `artifact_crs` must open them as such."""
    shutil.copy(FIXTURES / "aerial_rgb.tif", ws / "dop.tif")
    turn = _turn([("fetch_dop", _ok(ws / "dop.tif"))])
    a = {"kind": "artifact_crs", "tool": "fetch_dop", "crs": "EPSG:25832"}
    assert benchchecks.check_artifact(a, turn, ws)[0]
    count = {"kind": "artifact_count_in_range", "tool": "fetch_dop", "min": 1, "max": 9}
    assert "Raster" in benchchecks.check_artifact(count, turn, ws)[1]


def test_alternatives_qgis_outputs_and_team_tools(ws):
    """`a|b` as in tools_expected; QGIS writes `results.OUTPUT`; ressort tools count."""
    result = {"ok": True, "results": {"OUTPUT": str(ws / "stops_buffer.gpkg")}}
    qgis = _turn([("qgis_buffer", result)])
    a = {"kind": "artifact_crs", "tool": "vector_buffer|qgis_buffer", "crs": "EPSG:25832"}
    assert benchchecks.check_artifact(a, qgis, ws)[0]
    rec = {"checks": [{"kind": "tool_called", "tool": "vector_buffer|qgis_buffer"}]}
    assert benchchecks.evaluate(rec, qgis, ws)[0]
    assert not benchchecks.evaluate(rec, _turn([("geocode", {"ok": True})]), ws)[0]
    assert benchchecks.answer_number("Der Dom ist **105 Meter** hoch.", "Dom", "m|Meter") == 105


def test_a_run_without_a_session_is_read_from_the_run_log(tmp_path):
    """The `inspect_map` crash of 2026-11-07 left no session — the run log still has it."""
    from datetime import datetime, timedelta

    now = datetime.now().replace(microsecond=0)
    old, new = (now - timedelta(hours=2)).isoformat(), now.isoformat()
    rows = [  # an earlier run of the same key, then this one
        {"t": old, "kind": "call", "tool": "fetch_dem", "args": "{}"},
        {"t": new, "kind": "call", "tool": "fetch_dgm1", "args": '{"bbox": [1, 2, 3, 4]}'},
        {"t": new, "kind": "result", "tool": "fetch_dgm1",
         "result": '{"ok": true, "output": "/ws/dgm1.tif"}'},
        {"t": new, "kind": "text", "text": "Der Arbersee liegt auf 934,9 m."},
    ]
    (tmp_path / "testprompt_x.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    turn = benchchecks.turn_from_runlog("testprompt:x", now - timedelta(minutes=5), tmp_path)
    assert turn.tools == ["fetch_dgm1"]  # the earlier run's fetch_dem is not this run
    assert turn.written == ["/ws/dgm1.tif"]
    assert benchchecks.answer_number(turn.answer, "Arbersee", "m") == 934.9
    with pytest.raises(ValueError):
        benchchecks.turn_from_runlog("testprompt:x", now + timedelta(minutes=1), tmp_path)


PROTOCOL = """# x
model: m

11:45:33 +171.5s │ → fetch_swiss_boundaries({"level": "GEMEINDE", "canton": "Bern"})
11:45:40         │ ← fetch_swiss_boundaries: {"ok": true, "output": "/gone/b.gpkg", "units": 338}
11:46:02 + 29.5s │ → run_code({"code": "await vector_buffer(… (+1688 chars))
11:47:02 +  2.0s │ Hier ist die Karte der Gemeinden.
11:47:05 +  2.9s │ Der Arbersee liegt auf 934,9 m.

[run] 295s
"""


def test_a_judged_run_is_rebuilt_from_its_protocol(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(PROTOCOL, encoding="utf-8")
    turn = benchchecks.turn_from_protocol(log)
    assert turn.tools == ["fetch_swiss_boundaries", "run_code"]
    assert turn.tool_calls[0][1]["canton"] == "Bern"
    assert benchchecks.last_output(turn, "fetch_swiss_boundaries") == "/gone/b.gpkg"
    assert benchchecks.answer_number(turn.answer, "Arbersee", "m") == 934.9


def test_the_comparison_skips_what_cannot_be_checked_today(tmp_path):
    """A deleted file and a tool hidden in a program are not failures of the run."""
    log = tmp_path / "run.log"
    log.write_text(PROTOCOL, encoding="utf-8")
    bank = {"t": {"id": "t", "checks": [
        {"kind": "tool_called", "tool": "fetch_swiss_boundaries"},
        {"kind": "artifact_count_in_range", "tool": "fetch_swiss_boundaries", "min": 1, "max": 9},
        {"kind": "tool_called", "tool": "vector_buffer"},
        {"kind": "artifact_crs", "tool": "vector_buffer", "crs": "EPSG:25832"}]}}
    rec = {"test_id": "t", "log": str(log), "passed": "False", "ts": "2026-11-08T10:00",
           "tools_called": "['fetch_swiss_boundaries', 'run_code', 'vector_buffer']"}
    (row,) = benchchecks.compare_with_judge([rec], bank)
    assert row["mechanical"] and not row["judge"]  # two checkable, both pass
    qgis = {**rec, "tools_called": "['qgis_buffer']"}
    assert benchchecks.compare_with_judge([qgis], bank) == []  # QGIS-era runs are left out
