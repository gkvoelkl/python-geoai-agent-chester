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
    assert 'place="Regensburg, Bayern"' in notes
    # Not the exact float spelling: importing `cjio` (CityJSON) replaces the stdlib
    # JSON float encoder process-wide with a fixed ".6f" format, so the same call
    # renders as `12.0` or `12.000000` depending on whether a 3D test ran first in
    # the same process (found 2026-08-23 via tests/test_citymodel.py).
    assert "osm_features(bbox=[12" in notes and "49.1" in notes.replace("49.100000", "49.1")
    # Seit dem umgedrehten Filter (2026-09-04) erscheint auch `geocode(query=…)`:
    # gezeigt wird alles ausser Sperrgut, damit ein Kriterium ueber ein beliebiges
    # Argument beantwortbar bleibt.
    assert 'geocode(query="Regensburg")' in notes
    # `tags` erscheint jetzt ebenfalls — ein Kriterium wie "holt building=yes"
    # waere sonst nicht beantwortbar. Fruehere Fassung schloss es aus.
    assert 'tags={"a": "b"}' in notes


def test_scoping_notes_is_empty_only_when_everything_was_hidden(monkeypatch, tmp_path):
    """Die alte Fassung erwartete hier "", weil `qgis_buffer(distance=500)` keinen
    Eingrenzungsschlüssel trug. Seit dem umgedrehten Filter ist die Prämisse hinfällig
    — und das ist der Sinn der Sache: `distance` ist genau so ein Argument, nach dem
    ein Kriterium fragen kann ("500-m-Puffer"). Leer bleibt es nur, wenn wirklich
    nichts Zeigbares übrig ist."""
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    _session(tmp_path, "shown", [{"tool_name": "qgis_buffer", "args": {"distance": 500}}])
    assert "distance=500" in testprompt.scoping_notes("shown")

    _session(tmp_path, "none", [{"tool_name": "qgis_python", "args": {"code": "x = 1"}}])
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


@pytest.mark.llm
def test_ask_returns_the_validated_answer(tmp_path):
    """Der Rückweg, der vier Tage lang stumm war (SelmaKit ≥ 0.1.33).

    Bis 0.1.32 fing `run_stream_events` das `AgentRunResultEvent` ab und reichte es
    nicht weiter; `ask()` gab deshalb immer `None` zurück, und **jede** Anmerkung des
    Validierungs-Gates blieb für Protokoll, Trace und Judge unsichtbar. Auf Unit-Ebene
    war das nicht zu sehen — die Bausteine waren korrekt, nur nie verbunden. Dieser
    Test hängt einen Validator an, der unbedingt anhängt: Kommt die Marke zurück, ist
    der ganze Weg offen.
    """
    import asyncio

    from dotenv import load_dotenv
    from selmakit import Gateway

    from agent_build import (
        CONFIG_NAME,
        STATE_DIR,
        geo_capabilities,
        selmakit_capabilities,
    )
    from ask import ask
    from setup import setup

    setup(quiet=True)
    load_dotenv()
    agent = Gateway.from_config(
        STATE_DIR, CONFIG_NAME,
        capabilities=selmakit_capabilities,
        extra_capabilities=geo_capabilities(),
    ).agent

    marker = "‹validator-was-here›"

    @agent.output_validator
    async def _append_marker(ctx, output):  # noqa: ANN001 — Signatur gibt SelmaKit vor
        return f"{output}\n{marker}" if isinstance(output, str) else output

    out = asyncio.run(ask(agent, "Antworte nur mit: bereit.",
                          session_key="test:validated-answer", sink=lambda s: None))
    assert out and marker in out, (
        "ask() liefert nicht die validierte Fassung — dann ist die Gate-Notiz wieder "
        "unsichtbar (SelmaKit < 0.1.33 oder ein Rückschritt in run_stream_events)"
    )


def test_every_runner_that_keeps_a_protocol_also_keeps_the_gate_note():
    """A runner that logs the stream but not the returned answer loses the note.

    The note lives **only** in what the agent run returns; the stream carries the
    model's text and SelmaKit persists the pre-validator messages. That was fixed in
    `testprompt.py` and `evals.py` on 2026-08-27 — and forgotten in `test_app.py`,
    where it stayed broken until 2026-09-01: the same defect (a placeholder instead
    of the map path) was flagged in the CLI run of
    `pluvial-flow-accumulation-tegernheim` and silently absent from the bench run of
    the same prompt half an hour earlier.

    Keyed on `save_run_log` rather than on a list of files, so the **next** runner is
    covered too. Source-level on purpose: importing `test_app` pulls in Streamlit.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted(root.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "save_run_log(" not in src:
            continue
        tree = ast.parse(src)
        calls = {n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "save_run_log" not in calls:  # the module that *defines* it
            continue
        if "validation_note" not in calls:
            offenders.append(path.name)
    assert not offenders, (
        f"Runner speichert ein Protokoll ohne die Gate-Notiz: {offenders}. "
        "Die Notiz hängt am Rückgabewert des Laufs, nicht am Strom — "
        "`validation_note(final_answer)` aufrufen und anhängen."
    )


# ── Judge-Panel: Mehrheitsurteil über mehrere Modelle ────────────────────────
# Ein Judge dreimal zu fragen mittelt Streuung weg, nicht Schlagseite. Am
# 2026-09-01 hat derselbe Judge einen Ausschnitt bestanden, der die halbe Strasse
# verfehlte, und ein CRS-Kriterium durchfallen lassen, das seine eigene Begruendung
# bestaetigte — solche Fehler wiederholt er. Deshalb mehrere Herkunftslinien.


def _verdict(passed, crits, reason="r"):
    from testprompt import CriterionResult, Verdict

    return Verdict(
        criteria=[CriterionResult(text=f"K{i}", passed=p) for i, p in enumerate(crits)],
        passed=passed,
        reason=reason,
    )


def test_majority_decides_the_overall_verdict():
    from testprompt import _merge_verdicts

    merged, agr = _merge_verdicts([
        ("a", _verdict(True, [True, True])),
        ("b", _verdict(False, [True, False])),
        ("c", _verdict(True, [True, True])),
    ])
    assert merged.passed is True
    assert agr["unanimous"] is False
    assert agr["votes"] == {"a": True, "b": False, "c": True}
    assert "2/3" in agr["tally"]


def test_criteria_are_merged_position_by_position():
    """Je Kriterium eigene Mehrheit — ein Judge kann bei den Kriterien mit der
    Mehrheit gehen und beim Gesamturteil nicht; der Systemprompt laesst ihm dabei
    Spielraum ("every criterion *that matters*")."""
    from testprompt import _merge_verdicts

    merged, agr = _merge_verdicts([
        ("a", _verdict(False, [True, False, True])),
        ("b", _verdict(False, [True, False, False])),
        ("c", _verdict(False, [False, False, True])),
    ])
    assert [c.passed for c in merged.criteria] == [True, False, True]
    assert agr["split_criteria"] == ["K0", "K2"]  # nur die uneinigen


def test_a_split_verdict_is_marked_in_the_reason():
    """Ein geteiltes Urteil darf nicht wie ein einstimmiges aussehen — sonst
    verschwindet die Unsicherheit der Messung in einer glatten Zahl."""
    from testprompt import _merge_verdicts

    merged, _agr = _merge_verdicts([
        ("a", _verdict(True, [True], reason="sauber")),
        ("b", _verdict(False, [False])),
        ("c", _verdict(True, [True])),
    ])
    assert merged.reason.startswith("[2/3 für bestanden, geteilt]")
    assert "sauber" in merged.reason  # die Begründung der Mehrheit, nicht irgendeine


def test_a_unanimous_verdict_keeps_its_reason_clean():
    from testprompt import _merge_verdicts

    merged, agr = _merge_verdicts([
        ("a", _verdict(True, [True], reason="alles erfüllt")),
        ("b", _verdict(True, [True], reason="anders formuliert")),
    ])
    assert merged.reason == "alles erfüllt"
    assert agr["unanimous"] is True
    assert agr["split_criteria"] == []


def test_a_judge_that_returns_fewer_criteria_still_counts_where_it_spoke():
    """Ein schwaches Panelmitglied darf die Kriterien der anderen nicht kappen."""
    from testprompt import _merge_verdicts

    merged, _agr = _merge_verdicts([
        ("a", _verdict(True, [True, True, True])),
        ("b", _verdict(True, [True])),          # nur ein Kriterium geliefert
        ("c", _verdict(False, [False, True, True])),
    ])
    assert len(merged.criteria) == 3


def test_a_single_member_panel_behaves_like_one_judge():
    """Der Rückfall auf `evals.judge_model` muss dieselbe Note ergeben wie vorher."""
    from testprompt import _merge_verdicts

    merged, agr = _merge_verdicts([("solo", _verdict(False, [False, True], reason="knapp"))])
    assert merged.passed is False
    assert merged.reason == "knapp"
    assert agr["unanimous"] is True


def _cfg(tmp_path, monkeypatch, evals: dict):
    import json

    import testprompt

    (tmp_path / testprompt.CONFIG_NAME).write_text(
        json.dumps({"model": {"model": "ollama/subject"}, "evals": evals}), encoding="utf-8"
    )
    monkeypatch.setattr(testprompt, "STATE_DIR", str(tmp_path))
    return testprompt


def test_the_panel_is_the_default_and_single_reduces_it(monkeypatch, tmp_path):
    """Vorgabe sind alle Judges; ``single=True`` kürzt auf den ersten.

    Die Richtung ist Absicht: Das volle Panel liefert das genauere Urteil, weil die
    Fehler eines einzelnen Judges Schlagseite sind und keine Streuung — Wiederholung
    mittelt die nicht weg, verschiedene Herkunftslinien schon. Der Schalter existiert
    allein für die Zeit: gemessen 2,8 min gegen 17,3 min je Lauf, weil drei ~19-GB-
    Modelle nacheinander geladen werden müssen.
    """
    tp = _cfg(tmp_path, monkeypatch, {"judge_models": ["ollama/a", "ollama/b", "ollama/c"]})

    members, name, _mut, _sg = tp.build_judge_panel()
    assert [n for _a, n in members] == ["ollama/a", "ollama/b", "ollama/c"]
    assert " + " in name, "der Panelname muss alle Mitglieder nennen"

    one, one_name, _mut, _sg = tp.build_judge_panel(single=True)
    assert [n for _a, n in one] == ["ollama/a"], "der erste, nicht irgendeiner"
    assert one_name == "ollama/a"


def test_a_named_override_beats_the_panel(monkeypatch, tmp_path):
    """``--judge-model`` ist die dritte Möglichkeit: genau dieses eine Modell."""
    tp = _cfg(tmp_path, monkeypatch, {"judge_models": ["ollama/a", "ollama/b"]})

    members, name, _mut, _sg = tp.build_judge_panel("ollama/anderer")
    assert [n for _a, n in members] == ["ollama/anderer"] and name == "ollama/anderer"


def test_a_config_with_only_the_old_single_key_still_works(monkeypatch, tmp_path):
    """Rückwärtskompatibel: ``evals.judge_model`` ergibt ein Panel aus einem Mitglied.

    Ältere Konfigurationen — und jede, die den Umstieg nicht mitgemacht hat — dürfen
    nicht mit „kein Judge konfiguriert" abbrechen.
    """
    tp = _cfg(tmp_path, monkeypatch, {"judge_model": "ollama/alt"})

    members, name, _mut, _sg = tp.build_judge_panel()
    assert [n for _a, n in members] == ["ollama/alt"] and name == "ollama/alt"


def test_one_self_grading_member_taints_the_whole_panel(monkeypatch, tmp_path):
    """Ein einziger Selbstbenoter verdirbt das Mehrheitsurteil mit."""
    tp = _cfg(tmp_path, monkeypatch, {"judge_models": ["ollama/a", "ollama/subject"]})

    _members, _name, model_under_test, self_grading = tp.build_judge_panel()
    assert model_under_test == "ollama/subject"
    assert self_grading is True


def test_scoping_notes_carry_every_argument_a_criterion_may_ask_about(monkeypatch, tmp_path):
    """Zu enge Schlüsselmenge kostete einen korrekten Lauf sein Urteil.

    2026-09-04, `swiss-population-choropleth-bern`: Der Aufruf trug
    ``canton="Bern"`` und ``level="GEMEINDE"``, gezeigt wurde dem Judge nur die
    bbox — weil `_SCOPING_ARGS` bei ("place", "bbox") stand. Zwei von drei Judges
    liessen das Kriterium „setzt level=GEMEINDE und canton=Bern" durchfallen,
    richtig nach ihrer Beweislage und falsch über den Lauf. Ein herausgefiltertes
    Argument macht das Kriterium darüber unbeantwortbar: „nicht übergeben" ist von
    „nicht gezeigt" nicht zu unterscheiden.
    """
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    _session(tmp_path, "swiss", [
        {"tool_name": "fetch_swiss_boundaries",
         "args": {"bbox": [6.8, 46.3, 8.4, 47.3], "canton": "Bern",
                  "level": "GEMEINDE", "output_path": "b.gpkg"}},
        {"tool_name": "fetch_boundaries", "args": {"match": "Tegernheim"}},
        {"tool_name": "fetch_lod2", "args": {"state": "BY", "street": "Hollerweg"}},
    ])
    notes = testprompt.scoping_notes("swiss")
    for expected in ('canton="Bern"', 'level="GEMEINDE"', 'match="Tegernheim"',
                     'state="BY"', 'street="Hollerweg"'):
        assert expected in notes, f"{expected} fehlt — das Kriterium darüber wäre blind"
    assert "output_path" not in notes, "Ausgabepfade sind keine Eingrenzung"


def test_scoping_notes_show_arguments_by_default(monkeypatch, tmp_path):
    """Umgedrehter Filter: gezeigt wird alles, ausser Sperrgut.

    Eine Erlaubnisliste hatte bereits sechs Argumente uebersehen, nach denen
    Kriterien der Bank fragen (`amenity`, `feed`, `column`, `theme`,
    `mean_headway`, `type`) — dieselbe Bauart wie `_PATH_KEYS`, die dreimal an
    einem fehlenden Namen scheiterte, bevor sie abgeleitet wurde.
    """
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    _session(tmp_path, "wide", [
        {"tool_name": "render_map",
         "args": {"column": "einwohnerzahl", "scheme": "NaturalBreaks",
                  "output_path": "m.html"}},
        {"tool_name": "fetch_gtfs_stops", "args": {"feed": "de_nv"}},
        {"tool_name": "qgis_run", "args": {"algorithm_id": "grass:r.to.vect",
                                           "parameters": {"type": "line"}}},
    ])
    notes = testprompt.scoping_notes("wide")
    for expected in ('column="einwohnerzahl"', 'feed="de_nv"', 'grass:r.to.vect'):
        assert expected in notes, f"{expected} fehlt — das Kriterium darüber wäre blind"
    assert "output_path" not in notes and "m.html" not in notes


def test_write_plan_is_kept_out_of_the_transcript(monkeypatch, tmp_path):
    """Es wiederholt den ganzen Plan bei jedem Aufruf und sagt nichts über die
    Geo-Arbeit; sechs Planschreibvorgänge würden das Zeilenbudget auffressen."""
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    _session(tmp_path, "planned", [
        {"tool_name": "write_plan", "args": {"items": [{"content": "x", "id": "1"}]}},
        {"tool_name": "geocode", "args": {"query": "Bern"}},
    ])
    notes = testprompt.scoping_notes("planned")
    assert "write_plan" not in notes
    assert 'geocode(query="Bern")' in notes


def test_a_long_value_keeps_its_size(monkeypatch, tmp_path):
    """"3 items" beantwortet ein Kriterium über die Zahl gestapelter Ebenen;
    ein blosses Abschneiden nicht."""
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    _session(tmp_path, "long", [
        {"tool_name": "render_map", "args": {"layers": [f"{'x' * 40}{i}.gpkg" for i in range(3)]}},
    ])
    assert "(3 items)" in testprompt.scoping_notes("long")


def test_the_judge_is_told_to_score_the_call_whose_result_was_used():
    """Sonst bestraft die Bank genau die Selbstkorrektur, die sie auslösen will.

    Gemessen 2026-09-05 an drei Läufen von `swiss-population-choropleth-bern`: Einer
    setzte `canton="Bern"` sofort (338 Gemeinden, PASS). Einer griff daneben, bekam
    die Werkzeugwarnung, korrigierte auf `canton=` — Ergebnis ebenfalls 338 — und
    fiel trotzdem durch, weil zwei von drei Judges den *ersten* Aufruf zählten.
    Jeder Riegel dieses Projekts arbeitet nach dem Muster Fehlgriff → Rückmeldung →
    Korrektur; eine Rubrik, die den Fehlgriff zählt, misst Riegel als
    Verschlechterung. Vorbild ist das "Last-Attempt Alignment" der PEA-Metrik
    (GeoAgentBench, arXiv 2604.13888).
    """
    from testprompt import JUDGE_SYSTEM

    text = " ".join(JUDGE_SYSTEM.split())
    assert "whose result the run actually used" in text
    assert "self-correction" in text
    # Die Gegenprobe gehört dazu: ein falscher *Endzustand* bleibt ein Fehler.
    assert "wrong *final* state" in text
    # Und der Aufwand geht nicht verloren — er wird getrennt gezählt.
    assert "effort" in text
