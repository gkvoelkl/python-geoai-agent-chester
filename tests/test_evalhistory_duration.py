"""Late-added run fields in the eval history: aggregation and formatting (chester.evalhistory).

Both run duration and tool-call count were added after the log already had rows,
so the central question these cover is that a *partly* populated history stays
honest — a run archived before the field existed must not read as instant, nor as
a run that made zero tool calls.
"""

from __future__ import annotations

from chester import evalhistory


def _run(
    model, *, passed=True, duration=None, calls=None, test_id="t", ts="2026-07-27T10:00:00+00:00"
):
    record = {
        "ts": ts,
        "test_id": test_id,
        "model": model,
        "judge_model": "j",
        "passed": passed,
        "tool_coverage": 1.0,
    }
    if duration is not None:
        record["duration_s"] = duration
    if calls is not None:
        record["tool_calls"] = calls
    return record


def test_avg_duration_over_timed_runs_only():
    stats = evalhistory.per_model(
        [
            _run("m", duration=60.0),
            _run("m", duration=120.0),
            _run("m"),  # archived before timing existed
        ]
    )[0]
    assert stats["runs"] == 3
    assert stats["timed"] == 2
    assert stats["avg_duration"] == 90.0  # not 60.0 — the untimed run is excluded


def test_untimed_model_reports_unknown_duration():
    stats = evalhistory.per_model([_run("m"), _run("m")])[0]
    assert stats["avg_duration"] is None
    assert stats["timed"] == 0


def test_report_marks_partially_timed_models():
    text = evalhistory.format_report([_run("m", duration=60.0), _run("m")])
    assert "avg time" in text
    assert "60s (1)" in text  # mean plus how many runs it rests on


def test_report_omits_count_when_every_run_is_timed():
    text = evalhistory.format_report([_run("m", duration=60.0), _run("m", duration=60.0)])
    assert "60s |" in text
    assert "60s (" not in text


def test_duration_formatting_switches_to_minutes():
    assert evalhistory._fmt_dur(None) == "-"
    assert evalhistory._fmt_dur(42) == "42s"
    assert evalhistory._fmt_dur(89) == "89s"
    assert evalhistory._fmt_dur(450) == "7.5min"


def test_avg_calls_over_counted_runs_only():
    stats = evalhistory.per_model(
        [
            _run("m", calls=10),
            _run("m", calls=20),
            _run("m"),  # archived before tool_calls existed
        ]
    )[0]
    assert stats["counted"] == 2
    assert stats["avg_calls"] == 15.0  # not 10.0 — the uncounted run is excluded


def test_uncounted_model_reports_unknown_calls():
    stats = evalhistory.per_model([_run("m"), _run("m")])[0]
    assert stats["avg_calls"] is None
    assert stats["counted"] == 0


def test_report_marks_partially_counted_models():
    text = evalhistory.format_report([_run("m", calls=10), _run("m")])
    assert "avg calls" in text
    assert "10 (1)" in text  # mean plus how many runs it rests on


def test_latest_per_test_row_shows_dash_without_calls():
    text = evalhistory.format_report([_run("m", test_id="alpha")])
    assert "| `alpha` | ✓ PASS | 100% | - |" in text


def test_latest_per_test_row_carries_its_duration():
    text = evalhistory.format_report(
        [
            _run("m", test_id="alpha", duration=1800.0, ts="2026-07-27T10:00:00+00:00"),
        ]
    )
    assert "| `alpha` |" in text
    assert "30.0min" in text
