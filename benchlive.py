"""The bench's run view — from Chester's **own** protocol.

Up to SelmaKit 0.1.32 the bench showed a merged timeline: Chester's live protocol plus
SelmaKit's transcript view in one table. 0.1.33 removed that view ("make `/verbose` the
one instrumentation surface"), and it was **not rebuilt** — it had not proven itself.
When reading a run, what always counted was the timestamped protocol under
``.chester/evals/runs/``: it shows *where* the runtime went, and it survives the next
run, which the session file does not (decision 2026-08-31, `internal/selmakit-needs.md`
§2).

What remains is what Chester holds itself: find the kept protocols, match the one that
belongs to a graded run, and show one. The live stream of a running turn goes through
the same sink as in the CLI — one formatting, one event route (`ask.py`).
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
    """A kept run: header lines as a block, the raw protocol below.

    The protocol carries the time of day and the gap to the previous line on every line
    — exactly what counts when reading it back. The former transcript table beside it is
    gone (see the module docstring).
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
