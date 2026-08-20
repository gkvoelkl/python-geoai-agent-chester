"""Which test to run next: never used, else idle longest (testprompt).

The bench's counterpart to the 🎲 button. What makes it more than a sort is where
"used" comes from: **three** records outlive a run and none of them alone is
complete — the kept protocols only exist since 0.1.3, the eval history holds
judged runs only, and a session file is deleted by `--fresh` at the start of the
run it belongs to. Reading any one of them alone would call a test "never used"
that has run twenty times, and then propose it forever.

Measured against the real bank while building this: 36 tests, 5 genuinely never
run, and the oldest real use dates to 2026-07-17 — a month before the earliest
protocol. Protocols alone would have declared 30+ tests untouched.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from testprompt import last_used, pick_stalest_test


@pytest.fixture
def bank(tmp_path, monkeypatch):
    """Three empty record stores plus a helper to write into each of them."""
    import testprompt

    runs, sessions = tmp_path / "runs", tmp_path / "sessions"
    history = tmp_path / "history.jsonl"
    runs.mkdir()
    sessions.mkdir()
    monkeypatch.setattr(testprompt, "RUNS_DIR", runs)
    monkeypatch.setattr(testprompt, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(testprompt, "HISTORY_PATH", history)

    class Bank:
        def protocol(self, test_id, stamp="20260819T121114Z"):
            (runs / f"{stamp}__{test_id}.log").write_text("x")

        def history(self, test_id, ts="2026-08-18T10:00:00+00:00"):
            with history.open("a") as fh:
                fh.write(json.dumps({"test_id": test_id, "ts": ts}) + "\n")

        def session(self, test_id, *, prefix="testapp:", age_days=0.0):
            path = sessions / f"{prefix}{test_id}.json"
            path.write_text("[]")
            when = time.time() - age_days * 86400
            os.utime(path, (when, when))

    return Bank()


def _tests(*ids):
    return [{"id": i} for i in ids]


def test_a_never_used_test_wins(bank):
    bank.protocol("alt")
    assert pick_stalest_test(_tests("alt", "neu")) == "neu"


def test_among_never_used_tests_the_bank_order_decides(bank):
    """Stable, so pressing the button repeatedly sweeps the bank instead of jumping."""
    assert pick_stalest_test(_tests("eins", "zwei", "drei")) == "eins"


def test_with_everything_used_the_oldest_wins(bank):
    bank.protocol("frisch", "20260819T120000Z")
    bank.protocol("alt", "20260701T120000Z")
    assert pick_stalest_test(_tests("frisch", "alt")) == "alt"


def test_the_history_counts_as_use_on_its_own(bank):
    """A judged run from before protocols existed leaves *only* a history row."""
    bank.history("benotet")
    stamps = last_used(["benotet", "nie"])
    assert stamps["benotet"] > 0 and stamps["nie"] == 0
    assert pick_stalest_test(_tests("benotet", "nie")) == "nie"


def test_a_session_file_counts_as_use_on_its_own(bank):
    """The deepest record: unjudged bench runs leave nothing else behind."""
    bank.session("gelaufen")
    assert pick_stalest_test(_tests("gelaufen", "nie")) == "nie"


@pytest.mark.parametrize("prefix", ["testprompt:", "eval:", "testapp:"])
def test_every_runners_session_counts(bank, prefix):
    """CLI, batch and bench all count — "used" is not "used *here*"."""
    bank.session("gelaufen", prefix=prefix)
    assert last_used(["gelaufen"])["gelaufen"] > 0


def test_the_newest_of_the_three_sources_wins(bank):
    """Merging takes the maximum, so a stale source cannot age a fresh run.

    The case that matters in practice: a test whose session file is ancient because
    it was cleared long ago, but which ran again yesterday. Reading the session
    alone would keep proposing it ahead of tests that really are overdue.
    """
    now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bank.session("frisch_gelaufen", age_days=30)
    bank.protocol("frisch_gelaufen", now)
    bank.session("wirklich_alt", age_days=1)
    assert pick_stalest_test(_tests("frisch_gelaufen", "wirklich_alt")) == "wirklich_alt"


def test_a_foreign_session_key_is_ignored(bank):
    """`cli.json` is a chat, not a test run — it must not mark anything as used."""
    bank.session("test", prefix="")
    assert last_used(["test"])["test"] == 0


def test_meta_sidecars_do_not_count_as_a_run(bank):
    """`testapp:x.meta.json` sits next to every session; it is not a second run."""
    bank.session("anderer")  # damit das Verzeichnis wie im Betrieb belegt ist
    import testprompt

    (testprompt.SESSIONS_DIR / "testapp:test.meta.json").write_text("{}")
    assert last_used(["test"])["test"] == 0


def test_an_empty_bank_proposes_nothing(bank):
    assert pick_stalest_test([]) is None


def test_missing_stores_are_not_an_error(tmp_path, monkeypatch):
    """A fresh clone has no runs dir, no history and no sessions — everything is new."""
    import testprompt

    monkeypatch.setattr(testprompt, "RUNS_DIR", tmp_path / "nope")
    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path / "nada")
    monkeypatch.setattr(testprompt, "HISTORY_PATH", tmp_path / "keine.jsonl")
    assert pick_stalest_test(_tests("eins", "zwei")) == "eins"


def test_a_broken_history_line_does_not_hide_the_rest(bank):
    """Best-effort like every other history reader: one bad line, not a dead picker."""
    import testprompt

    testprompt.HISTORY_PATH.write_text('{kaputt\n{"test_id": "gut", "ts": "2026-08-18T10:00:00"}\n')
    assert last_used(["gut"])["gut"] > 0
