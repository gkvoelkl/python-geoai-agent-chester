"""The bench finds its kept logs again.

Of this module exactly that is left: list runs, match the log to a graded run, show one.
The merged timeline (Chester's log plus SelmaKit's transcript view) went away with
SelmaKit 0.1.33 and was **not** rebuilt — it had not proven itself; when reading back,
what always counted was the timestamped log (`internal/selmakit-needs.md` §2).

The matching is the part with the trap: entries from before the logs have no `log`
field, and the fallback rule must hit the run *before* archiving — the timestamp is
taken after the run, never before.
"""

from __future__ import annotations

from pathlib import Path

import benchlive


def _runs_dir(root: Path, *stems: str) -> Path:
    """A log directory with the named runs (empty files are enough)."""
    root.mkdir(parents=True, exist_ok=True)
    for stem in stems:
        (root / f"{stem}.log").write_text("", encoding="utf-8")
    return root


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
