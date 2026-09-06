"""Die Laufansicht der Bench — aus Chesters **eigenem** Protokoll.

Bis SelmaKit 0.1.32 zeigte die Bench eine zusammengeführte Zeitleiste: Chesters
Live-Protokoll plus SelmaKits Transcript-Ansicht in einer Tabelle. 0.1.33 hat diese
Ansicht entfernt („make `/verbose` the one instrumentation surface"), und sie wurde
**nicht nachgebaut** — sie hatte sich nicht bewährt. Beim Lesen eines Laufs zählte
immer das zeitgestempelte Protokoll unter ``.chester/evals/runs/``: Dort steht, *wo*
die Laufzeit hinging, und es überlebt den nächsten Lauf, was die Sitzungsdatei nicht
tut (Entscheidung 2026-08-31, `internal/selmakit-needs.md` §2).

Übrig bleibt, was Chester selbst hält: die aufbewahrten Protokolle finden, das zu
einem benoteten Lauf gehörige zuordnen, und eines anzeigen. Der Live-Strom eines
laufenden Zuges geht über denselben Sink wie in der CLI — eine Formatierung, ein
Ereignisweg (`ask.py`).
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st


def run_logs(runs_dir: str | Path) -> list[Path]:
    """Every kept run protocol, newest first."""
    return sorted(Path(runs_dir).glob("*.log"), reverse=True)


def log_for(runs_dir: str | Path, record: dict) -> Path | None:
    """The protocol belonging to a judged run.

    Records written before the protocol existed have no ``log`` field, so fall back
    to the newest file for that test that started *before* the run was archived —
    the archive timestamp is taken after the run, never before it.
    """
    named = record.get("log")
    if named and Path(named).exists():
        return Path(named)
    test_id, ts = record.get("test_id", ""), str(record.get("ts", ""))
    stamp = ts.replace("-", "").replace(":", "")[:15]
    candidates = [p for p in run_logs(runs_dir) if p.stem.endswith(f"__{test_id}")]
    earlier = [p for p in candidates if p.stem[:15] <= stamp]
    return earlier[0] if earlier else None


def render_past_run(log_path: str | Path) -> None:
    """Ein aufbewahrter Lauf: Kopfzeilen als Block, darunter das Rohprotokoll.

    Das Protokoll trägt je Zeile Uhrzeit und Abstand zur vorigen — genau das, was
    beim Nachlesen zählt. Die frühere Transcript-Tabelle daneben ist entfallen
    (siehe Modul-Docstring).
    """
    path = Path(log_path)
    if not path.exists():
        st.caption(f"Protokoll nicht gefunden: `{path}`")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    header, _, body = text.partition("\n\n")
    st.code(header, language="yaml")
    st.markdown(f"**Protokoll** — `{path}`")
    st.code(body or text)
