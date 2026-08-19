"""The kept per-run protocol: timestamped lines, and one file per run (testprompt).

Why kept at all: the session file is written per *session key*, so the next run of
the same test overwrites it and `--fresh` deletes it — the record of a run survived
exactly until the run after it. Asked for on 2026-08-16 after a benchmark run was
abandoned and there was nothing left to look at.
"""

from __future__ import annotations

import json

from testprompt import save_run_log, timestamped_sink


def test_every_line_gets_a_clock_time():
    out: list[str] = []
    sink = timestamped_sink(out.append)
    sink("erste Zeile\n")
    sink("zweite Zeile\n")
    text = "".join(out)
    assert text.count("│") == 2, "je Zeile ein Stempel, nicht je Chunk"
    for line in text.splitlines():
        assert line[2] == ":" and line[5] == ":", f"keine Uhrzeit am Zeilenanfang: {line!r}"


def test_a_line_split_across_chunks_is_stamped_once():
    # Tokens arrive in fragments; stamping per chunk would pepper one line with times.
    out: list[str] = []
    sink = timestamped_sink(out.append)
    for piece in ("eine ", "Zeile ", "in Teilen\n"):
        sink(piece)
    assert "".join(out).count("│") == 1


def test_blank_lines_are_not_stamped():
    out: list[str] = []
    sink = timestamped_sink(out.append)
    sink("\n\n")
    assert "│" not in "".join(out)


def test_sink_tees_to_every_writer():
    a: list[str] = []
    b: list[str] = []
    timestamped_sink(a.append, b.append)("hallo\n")
    assert "".join(a) == "".join(b) and "hallo" in "".join(a)


def test_run_log_keeps_header_and_body(tmp_path, monkeypatch):
    import testprompt

    monkeypatch.setattr(testprompt, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path / "sessions")
    path = save_run_log("my-test", "ollama/x", "testprompt:my-test", "PROTOKOLL", duration_s=12.3)
    text = path.read_text()
    assert path.name.endswith("__my-test.log")
    assert "model:       ollama/x" in text
    assert "session_key: testprompt:my-test" in text
    assert "duration_s:  12.3" in text
    assert text.endswith("PROTOKOLL")


def test_two_runs_of_one_test_do_not_overwrite_each_other(tmp_path, monkeypatch):
    """The whole point: a second run must not erase the first one's record."""
    import testprompt

    monkeypatch.setattr(testprompt, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path / "sessions")
    stamps = iter(["20260816T090000Z", "20260816T100000Z"])

    class _Clock:
        @staticmethod
        def now(tz=None):
            class _T:
                @staticmethod
                def strftime(_fmt):
                    return next(stamps)

            return _T()

    monkeypatch.setattr(testprompt, "datetime", _Clock)
    first = save_run_log("t", "m", "k", "lauf eins")
    second = save_run_log("t", "m", "k", "lauf zwei")
    assert first != second
    assert first.read_text().endswith("lauf eins")
    assert second.read_text().endswith("lauf zwei")


def test_the_session_trace_is_copied_next_to_the_log(tmp_path, monkeypatch):
    """Without the copy, the transcript of a past run cannot be rebuilt."""
    import testprompt

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "k.json").write_text(json.dumps([{"parts": []}]))
    monkeypatch.setattr(testprompt, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(testprompt, "SESSIONS_DIR", sessions)
    path = save_run_log("t", "m", "k", "log")
    copy = path.parent / f"{path.stem}.trace.json"
    assert copy.exists(), "ohne Trace-Kopie ist das Transcript eines alten Laufs verloren"
    assert json.loads(copy.read_text()) == [{"parts": []}]


def test_a_missing_session_trace_does_not_break_archiving(tmp_path, monkeypatch):
    import testprompt

    monkeypatch.setattr(testprompt, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path / "nope")
    assert save_run_log("t", "m", "k", "log").exists()
