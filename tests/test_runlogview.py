"""A run that never finished can still be read — from its run log.

SelmaKit writes the session file only when a turn ends; a run cut off by a time limit,
a crash or an exhausted API credit leaves none, and `trace.py <key>` said "No such
session" for exactly the runs worth reading (2026-08-16, and every probe that hits its
time limit). The run log is appended per call and survives; these tests hold that it
is read back, and that the most recent run is cut out of a log that keeps them all.
"""

from __future__ import annotations

import json
import trace as trace_cli  # the repo's trace.py, not the stdlib module (repo root wins)

from chester.runtime import runlogview


def _rec(t, kind, tool="vector_info", **kw):
    return {"t": f"2026-10-04T{t}", "kind": kind, "tool": tool, **kw}


def test_the_last_run_starts_after_the_last_long_pause():
    records = [_rec("10:00:00", "call"), _rec("10:00:01", "result", seconds=1.0),
               _rec("11:00:00", "call", tool="geocode"), _rec("11:00:02", "result", tool="geocode")]
    assert [r["tool"] for r in runlogview.last_run(records)] == ["geocode", "geocode"]


def test_a_long_pause_that_ends_in_a_result_is_the_same_run():
    """The Opus run of 2026-10-04 hung 2 h 23 min inside one call — still one run."""
    records = [_rec("16:57:41", "call", tool="memory_search"),
               _rec("16:58:19", "call", tool="geo_python_run"),
               _rec("19:21:39", "result", tool="geo_python_run", seconds=8599.6),
               _rec("19:21:44", "call", tool="vector_overlay")]
    assert runlogview.last_run(records)[0]["tool"] == "memory_search"


def test_a_half_written_last_line_is_skipped(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text(json.dumps(_rec("10:00:00", "call")) + '\n{"t": "2026-10-04T10:0', "utf-8")
    assert len(runlogview.read_records(path)) == 1


def test_trace_shows_the_run_log_when_the_session_is_missing(tmp_path, monkeypatch, capsys):
    logs = tmp_path / "runs"
    logs.mkdir()
    key = "probe:cut-off"
    (logs / "probe_cut-off.jsonl").write_text(
        json.dumps(_rec("10:00:00", "call", tool="vector_buffer", args='{"distance": 500}'))
        + "\n", "utf-8")
    monkeypatch.setattr(trace_cli, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(trace_cli, "log_path",
                        lambda k: runlogview.log_path(k, log_dir=str(logs)))
    trace_cli.show(key, full=False, show_system=False)
    out = capsys.readouterr().out
    assert "no session" in out and "vector_buffer" in out and "distance" in out
