"""A run that cannot be read back must not be graded (testprompt).

Both guards come from one incident, 2026-08-16: a benchmark run wrote its Sentinel
bands, its NDVI map and a validation snapshot to the GeoCache over 954 s, but no
session file appeared. `read_trace` reported "no tools, no answer", the judge graded
that faithfully, and `history.jsonl` gained a FAIL whose stated reason — "the agent
produced no tool calls" — was false and completely convincing.

The rule these encode: a broken measurement must look broken, not like a finding.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from testprompt import TraceUnavailable, judge_run, read_trace, trace_from_protocol

# One real run that died mid-stream: `inspect_map` handed its snapshot to a text-only
# model, Ollama rejected the request, and SelmaKit — which persists only a *completed*
# run — wrote no session at all (2026-08-19, `walk-isochrone-hauptbahnhof`).
_ABORTED_PROTOCOL = """\
14:03:17 +  9.6s │ → geocode({"query":"Regensburg Hbf"})
14:03:17         │ ← geocode: {"ok": true, "display_name": "Regensburg Hauptbahnhof"}
14:09:27 + 68.5s │ → qgis_service_area({"minutes":15,"mode":"walk"})
14:09:36 +  9.3s │ ← qgis_service_area: {"ok": true, "reach_distance_m": 1125}
14:11:14 +  0.1s │ [run error: ModelHTTPError: status_code: 400, model_name: gemma4]
"""


def _judge_stub():
    """Stands in for the judge agent; fails the test if it is ever consulted."""

    class _Never:
        async def run(self, _prompt):  # pragma: no cover - reaching it is the failure
            raise AssertionError("judge was called for a transcript that cannot be graded")

    return _Never()


def test_missing_trace_raises_instead_of_reading_as_an_idle_run(monkeypatch, tmp_path):
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    with pytest.raises(TraceUnavailable) as exc:
        read_trace("never-persisted")
    # The message must point at the real suspect: reading, not the run.
    assert "never-persisted" in str(exc.value)


def test_unreadable_trace_raises_too(monkeypatch, tmp_path):
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    (tmp_path / "broken.json").write_text("{not json")
    with pytest.raises(TraceUnavailable):
        read_trace("broken")


def test_a_real_trace_still_reads(monkeypatch, tmp_path):
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    (tmp_path / "ok.json").write_text(
        json.dumps(
            [
                {"parts": [{"part_kind": "tool-call", "tool_name": "geocode"}]},
                {"parts": [{"part_kind": "text", "content": "56,6 km."}]},
            ]
        )
    )
    tools, answer = read_trace("ok")
    assert tools == ["geocode"] and answer == "56,6 km."


def test_a_crashed_run_is_graded_from_its_protocol(monkeypatch, tmp_path):
    """No session file, but the streamed protocol is right there — use it.

    Without this the 634 s of real geoprocessing in that run were unreadable and
    ungradable, purely because the turn ended in an exception instead of a result.
    """
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    tools, answer = read_trace("never-persisted", _ABORTED_PROTOCOL)
    assert tools == ["geocode", "qgis_service_area"]
    # The abort must be *stated*, not left as an empty answer the judge would read
    # as "the model said nothing" — that conflation is what this module guards.
    assert "aborted" in answer and "ModelHTTPError" in answer


def test_a_protocol_without_tools_or_error_still_raises(monkeypatch, tmp_path):
    """The fallback may not invent a transcript out of plain streamed text."""
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    with pytest.raises(TraceUnavailable):
        read_trace("never-persisted", "14:03:17 │ Ich schaue mir das an.\n")


def test_the_session_file_wins_when_both_exist(monkeypatch, tmp_path):
    """The persisted trace is the record; the protocol is only its stand-in."""
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    (tmp_path / "ok.json").write_text(
        json.dumps([{"parts": [{"part_kind": "text", "content": "56,6 km."}]}])
    )
    assert read_trace("ok", _ABORTED_PROTOCOL) == ([], "56,6 km.")


def test_protocol_parsing_ignores_tool_results():
    """Only the `→` call lines are the sequence; `←` results would double every tool."""
    tools, _ = trace_from_protocol(_ABORTED_PROTOCOL)
    assert tools == ["geocode", "qgis_service_area"]


def test_judge_refuses_an_empty_transcript():
    with pytest.raises(TraceUnavailable):
        asyncio.run(judge_run(_judge_stub(), {"id": "x"}, "prompt", [], ""))


def test_judge_refuses_whitespace_as_an_answer():
    with pytest.raises(TraceUnavailable):
        asyncio.run(judge_run(_judge_stub(), {"id": "x"}, "prompt", [], "   \n "))


def test_an_answer_without_tools_is_still_gradable():
    """Not every valid run calls a tool — refusing those would hide real failures."""

    class _Verdict:
        criteria: list = []
        passed = False
        reason = "no tools were needed but the answer is wrong"

    class _Judge:
        async def run(self, _prompt):
            return type("R", (), {"output": _Verdict()})()

    verdict, coverage, missing, effort = asyncio.run(
        judge_run(_Judge(), {"id": "x", "tools_expected": []}, "prompt", [], "eine Antwort")
    )
    assert verdict.passed is False
    assert effort["calls"] == 0


# ── scoping_notes: the argument the judge must not guess ─────────────────────


def _session(tmp_path, name, calls):
    (tmp_path / f"{name}.json").write_text(
        json.dumps([{"parts": [{"part_kind": "tool-call", **c} for c in calls]}])
    )


def test_scoping_notes_names_place_and_bbox_verbatim(monkeypatch, tmp_path):
    """A criterion about arguments cannot be graded from tool names.

    Measured 2026-08-23: `restaurant-heatmap` fetched with
    ``place="Regensburg, Bayern, Deutschland"``, and the judge — which sees names
    only — wrote "the double use of geocode followed by osm_features strongly
    suggests a bounding-box-based extraction" and failed the boundary criterion.
    A false FAIL on a correct run.
    """
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    _session(tmp_path, "mixed", [
        {"tool_name": "geocode", "args": {"query": "Regensburg"}},
        {"tool_name": "osm_features", "args": {"place": "Regensburg, Bayern", "tags": {"a": "b"}}},
        {"tool_name": "osm_features", "args": '{"bbox": [12.0, 48.9, 12.2, 49.1]}'},
    ])
    notes = testprompt.scoping_notes("mixed")
    assert 'osm_features(place="Regensburg, Bayern")' in notes
    # Not the exact float spelling: importing `cjio` (CityJSON) replaces the stdlib
    # JSON float encoder process-wide with a fixed ".6f" format, so the same call
    # renders as `12.0` or `12.000000` depending on whether a 3D test ran first in
    # the same process (found 2026-08-23 via tests/test_citymodel.py).
    assert "osm_features(bbox=[12" in notes and "49.1" in notes.replace("49.100000", "49.1")
    assert "geocode" not in notes, "nur Aufrufe mit place/bbox, sonst blaeht es den Prompt"
    assert "tags" not in notes, "nur die beiden Ausdehnungs-Argumente"


def test_scoping_notes_is_empty_when_nothing_scoped(monkeypatch, tmp_path):
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    _session(tmp_path, "none", [{"tool_name": "qgis_buffer", "args": {"distance": 500}}])
    assert testprompt.scoping_notes("none") == ""


def test_scoping_notes_survives_a_missing_session(monkeypatch, tmp_path):
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    assert testprompt.scoping_notes("never-written") == ""


def test_layer_facts_reports_crs_of_what_the_run_produced(monkeypatch, tmp_path):
    """The judge cannot see a coordinate system, and several tests grade one.

    Measured 2026-08-23 (`gtfs-stops-departures-map-regensburg`): the delivered layer
    was EPSG:25832 and the judge ticked the "Haltestellen in EPSG:4326" criterion —
    a false PASS, the mirror image of the false FAIL that `scoping_notes` fixed. Both
    come from the same habit: asked for a fact it cannot see, the judge guesses.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    wgs = tmp_path / "stops.gpkg"
    utm = tmp_path / "stops_clipped.gpkg"
    gpd.GeoDataFrame({"n": [1, 2]}, geometry=[Point(12.1, 49.0), Point(12.2, 49.1)],
                     crs="EPSG:4326").to_file(wgs, driver="GPKG")
    gpd.GeoDataFrame({"n": [1]}, geometry=[Point(721000, 5428000)],
                     crs="EPSG:25832").to_file(utm, driver="GPKG")
    (tmp_path / "run.json").write_text(json.dumps([{"parts": [
        {"part_kind": "tool-return", "tool_name": "fetch_gtfs_stops",
         "content": {"ok": True, "output": str(wgs)}},
        {"part_kind": "tool-return", "tool_name": "qgis_clip",
         "content": {"ok": True, "results": {"OUTPUT": str(utm)}}},
        {"part_kind": "tool-return", "tool_name": "render_map",
         "content": {"ok": True, "output": str(tmp_path / "map.html")}},
    ]}]))

    facts = testprompt.layer_facts("run")
    assert "stops.gpkg: EPSG:4326, 2 features" in facts
    assert facts.strip().endswith("stops_clipped.gpkg: EPSG:25832, 1 features"), (
        "das gelieferte Ergebnis muss die letzte Zeile sein"
    )
    assert "map.html" not in facts, "eine HTML-Karte hat kein CRS"


# ── the gate note the record used to swallow ─────────────────────────────────
# Found 2026-08-27 on `mean-elevation-per-district`: the answer linked
# `.chester/workspace/geocach/…html` — one letter short — and the validation gate
# *did* flag it. Nothing recorded that. The gate's advisory tier appends to the
# **returned** answer, while the stream carries the model's text and SelmaKit
# persists the pre-validator messages, so protocol, trace and judge all missed it.


def test_validation_note_is_extracted_from_the_returned_answer():
    from testprompt import validation_note

    answer = (
        "Die Karte liegt unter map.html.\n\n"
        "> 🔎 Validation note (level 1, advisory) — reported file(s) not found on disk "
        "— `map.html` (the result may not have been produced). "
        "Re-check the step, or ignore it if the result is right."
    )
    note = validation_note(answer)
    assert note is not None
    assert "not found on disk" in note
    assert "map.html" in note
    assert note.startswith("(level 1, advisory)")  # the marker itself is stripped


def test_a_clean_answer_carries_no_note():
    from testprompt import validation_note

    assert validation_note("Die mittlere Höhe beträgt 354,4 m.") is None
    assert validation_note(None) is None  # a run that produced no result at all
