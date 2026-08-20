"""The bench's merged run view: one timed timeline instead of log *and* transcript.

Both halves are covered here because both were wrong in an obvious way before:
a stream event that no branch handles is silently dropped (that is what hid the
model's reasoning — 132 thinking deltas per turn arrive before the first visible
character), and a merge that keeps the persisted copy of the streamed turn shows
every row of it twice.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # `importorskip` below is a value, not a name mypy can annotate with
    from benchlive import LiveRun

benchlive = pytest.importorskip("benchlive", reason="Streamlit fehlt")


class _Box:
    """A Streamlit placeholder that renders nowhere."""

    def container(self):
        return contextlib.nullcontext()


@pytest.fixture(autouse=True)
def _silent_streamlit(monkeypatch):
    """No script run context in pytest — swallow what the renderer emits."""

    class _St:
        html = staticmethod(lambda body: None)
        caption = staticmethod(lambda text: None)

    monkeypatch.setattr(benchlive, "st", _St)


def _run() -> LiveRun:
    live = benchlive.LiveRun(_Box(), throttle=0.0)
    live.start("Wie viele Haltestellen?")
    live.on_event("thinking", {"text": "Die Grenze ist "})
    live.on_event("thinking", {"text": "nicht in OSM."})
    live.on_event("text", {"text": "Ich hole sie amtlich."})
    live.on_event("tool_call", {"name": "wfs_features", "args": {"place": "Regensburg"}})
    live.on_event("tool_result", {"name": "wfs_features", "result": {"ok": True}})
    live.on_event("tool_call", {"name": "fetch_osm", "args": {"tags": {}}})
    live.on_event("tool_result", {"name": "fetch_osm", "result": "boom", "error": True})
    return live


def test_every_stream_event_becomes_a_row():
    kinds = [row.kind for row in _run().rows]
    assert kinds == ["USER", "THINKING", "ASSISTANT", "TOOL", "TOOL"], (
        f"Ereignis ohne Zeile verschluckt: {kinds}"
    )


def test_fragments_of_one_block_stay_one_row():
    thinking = next(row for row in _run().rows if row.kind == "THINKING")
    assert thinking.full == "Die Grenze ist nicht in OSM.", "Reasoning-Fragmente je Zeile gerendert"


def test_result_is_folded_into_its_call_row():
    tools = [row for row in _run().rows if row.kind == "TOOL"]
    assert tools[0].result and tools[0].mono.startswith("wfs_features")
    assert tools[1].error, "Retry-Prompt muss als Fehler markiert sein, nicht als Ergebnis"
    assert "boom" in tools[1].full


def test_a_tool_row_is_stamped_with_its_duration():
    live = _run()
    stamps = [t for row, t in zip(live.rows, live.times) if row.kind == "TOOL"]
    assert all("⏱" in s for s in stamps), f"Tool-Zeile ohne Laufzeit: {stamps}"
    prose = [t for row, t in zip(live.rows, live.times) if row.kind != "TOOL"]
    assert all(":" in s for s in prose), "Prosa-Zeile ohne Uhrzeit"


def _session(tmp_path, key: str = "s") -> str:
    """Two turns in pydantic-ai shape, instructions only on the first request."""
    messages = [
        {
            "kind": "request",
            "run_id": "r1",
            "instructions": "system\n\n## Skills\nskills text",
            "parts": [{"part_kind": "user-prompt", "content": "erste Frage"}],
        },
        {"kind": "response", "run_id": "r1", "parts": [{"part_kind": "text", "content": "erste"}]},
        {
            "kind": "request",
            "run_id": "r2",
            "parts": [{"part_kind": "user-prompt", "content": "zweite Frage"}],
        },
        {"kind": "response", "run_id": "r2", "parts": [{"part_kind": "text", "content": "zweite"}]},
    ]
    (tmp_path / f"{key}.json").write_text(json.dumps(messages), encoding="utf-8")
    return str(tmp_path)


def test_merge_replaces_the_streamed_turn_and_keeps_the_model_input(tmp_path):
    live = benchlive.LiveRun(_Box(), throttle=0.0)
    live.start("zweite Frage")
    rows, times = benchlive.merged(_session(tmp_path), "s", live.rows, live.times)
    kinds = [row.kind for row in rows]
    assert kinds.count("SYSTEM") == 1 and "CONTEXT" in kinds, "Modell-Eingabe fehlt"
    assert [row.text for row in rows].count("zweite Frage") == 1, "letzter Turn doppelt"
    assert len(times) == len(rows) and times[-1], "Live-Zeile ohne Zeitstempel"


def test_merge_without_a_live_capture_falls_back_to_the_session(tmp_path):
    rows, times = benchlive.merged(_session(tmp_path), "s", [], [])
    assert [row.text for row in rows].count("zweite Frage") == 1, "alter Lauf nicht mehr lesbar"
    assert times == []


def test_merge_without_a_session_file_keeps_the_live_rows(tmp_path):
    live = _run()
    rows, _ = benchlive.merged(str(tmp_path), "fehlt", live.rows, live.times)
    assert rows == live.rows, "ohne Session-Datei muss der gestreamte Lauf stehen bleiben"


# ── a past run ────────────────────────────────────────────────────────────────

# One turn with a tool call: request · response (thinking + call) · return · answer.
# The return message carries no row of its own — that is what shifted the time
# column by one and dropped it entirely.
_PAST = [
    {
        "kind": "request",
        "run_id": "r1",
        "timestamp": "2026-08-17T14:00:00Z",
        "instructions": "system",
        "parts": [{"part_kind": "user-prompt", "content": "frage"}],
    },
    {
        "kind": "response",
        "run_id": "r1",
        "timestamp": "2026-08-17T14:02:00Z",
        "parts": [
            {"part_kind": "thinking", "content": "überlegen"},
            {"part_kind": "tool-call", "tool_name": "geocode", "tool_call_id": "c1", "args": {}},
        ],
    },
    {
        "kind": "request",
        "run_id": "r1",
        "timestamp": "2026-08-17T14:03:00Z",
        "parts": [{"part_kind": "tool-return", "tool_call_id": "c1", "content": "ok"}],
    },
    {
        "kind": "response",
        "run_id": "r1",
        "timestamp": "2026-08-17T14:03:30Z",
        "parts": [{"part_kind": "text", "content": "antwort"}],
    },
]


def test_a_past_run_keeps_one_time_cell_per_message():
    rows, times = benchlive.timed_rows(_PAST)
    assert len(times) == len(rows), "Zeitspalte verworfen — Zuordnung Zeile→Nachricht kaputt"
    stamped = [(row.kind, t) for row, t in zip(rows, times) if t]
    kinds = [kind for kind, _ in stamped]
    assert kinds == ["SYSTEM", "THINKING", "ASSISTANT"], f"Stempel an falscher Zeile: {kinds}"
    assert "+120.0s" in stamped[1][1], "Wartezeit vor der Antwort fehlt"
    assert "+30.0s" in stamped[2][1], "Tool-Rückgabe hat die Uhr nicht weitergestellt"


def test_a_tool_row_of_a_past_run_claims_no_runtime():
    # The messages cannot separate generation from tool execution; pretending they
    # can was the first version of this and it read 67.5 s for a 3.7 s call.
    _, times = benchlive.timed_rows(_PAST)
    assert not any("⏱" in t for t in times), "erfundene Tool-Laufzeit im Protokoll"


def test_a_resent_instructions_block_collapses_to_what_changed():
    # A 26-minute run re-sent the blob six times, each time only because the
    # GeoCache section had grown — 132 of 212 rows were that repetition.
    first = "system\n\n## GeoCache\nleer\n\n## QGIS\nunverändert"
    second = "system\n\n## GeoCache\nein Layer\n\n## QGIS\nunverändert"
    messages = [
        {"kind": "request", "run_id": "r1", "instructions": first, "parts": []},
        {
            "kind": "request",
            "run_id": "r1",
            "instructions": second,
            "parts": [{"part_kind": "user-prompt", "content": "frage"}],
        },
    ]
    rows, _ = benchlive.timed_rows(messages)
    summary = [r for r in rows if r.full.startswith("Instruktionen")]
    assert len(summary) == 1, f"Wiederholung nicht zusammengefasst: {[r.kind for r in rows]}"
    assert "GeoCache" in summary[0].full and "QGIS" not in summary[0].full
    verbose, _ = benchlive.timed_rows(messages, collapse_repeats=False)
    assert len(verbose) > len(rows), "collapse_repeats=False muss den Rohblock zeigen"


def _runs_dir(tmp_path, *stems):
    for stem in stems:
        (tmp_path / f"{stem}.log").write_text("# log\n\nkörper", encoding="utf-8")
    return tmp_path


def test_log_lookup_prefers_the_recorded_path(tmp_path):
    kept = _runs_dir(tmp_path, "20260817T142545Z__t") / "20260817T142545Z__t.log"
    found = benchlive.log_for(tmp_path, {"test_id": "t", "log": str(kept)})
    assert found == kept


def test_log_lookup_falls_back_to_the_run_before_the_archive(tmp_path):
    # Old records predate the `log` field; the archive stamp is taken after the run.
    _runs_dir(tmp_path, "20260817T140000Z__t", "20260817T160000Z__t", "20260817T140000Z__anderer")
    found = benchlive.log_for(tmp_path, {"test_id": "t", "ts": "2026-08-17T15:00:00+00:00"})
    assert found and found.stem == "20260817T140000Z__t", f"falscher Lauf gewählt: {found}"


def test_log_lookup_gives_up_when_nothing_matches(tmp_path):
    _runs_dir(tmp_path, "20260817T140000Z__anderer")
    assert benchlive.log_for(tmp_path, {"test_id": "t", "ts": "2026-08-17T15:00:00+00:00"}) is None
