"""Die Bench findet ihre aufbewahrten Protokolle wieder.

Von diesem Modul ist genau das übrig: Läufe auflisten, das Protokoll zu einem
benoteten Lauf zuordnen, eines anzeigen. Die zusammengeführte Zeitleiste (Chesters
Protokoll plus SelmaKits Transcript-Ansicht) ist mit SelmaKit 0.1.33 entfallen und
**nicht** nachgebaut worden — sie hatte sich nicht bewährt, beim Nachlesen zählte
immer das zeitgestempelte Protokoll (`internal/selmakit-needs.md` §2).

Die Zuordnung ist der Teil mit der Falle: Einträge aus der Zeit vor den Protokollen
haben kein `log`-Feld, und die Rückfallregel muss den Lauf *vor* der Archivierung
treffen — der Zeitstempel wird nach dem Lauf genommen, nie davor.
"""

from __future__ import annotations

from pathlib import Path

import benchlive


def _runs_dir(root: Path, *stems: str) -> Path:
    """Ein Protokollverzeichnis mit den genannten Läufen (leere Dateien genügen)."""
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
