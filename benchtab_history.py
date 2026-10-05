"""History — past judged runs, their protocols and transcripts.

One tab of the test bench (`test_app.py`), split out on 2026-10-05; the body is the
former `with tab_hist:` block, unchanged.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

# All the heavy lifting is imported from the existing CLI runners — the UI is a
# thin skin so the bench can never drift from `testprompt.py` / `evals.py`.
from benchlive import log_for, render_past_run, run_logs
from benchshared import (
    _mine,
)
from chester import evalhistory
from testprompt import (
    RUNS_DIR,
)


def render() -> None:
    records = _mine(evalhistory.load_history())
    if not records:
        st.info("No judged runs yet. Run a test with *Judge* enabled to populate the history.")
    else:
        flt = st.text_input("Filter (test id / model substring)", key="hist_filter").strip()
        st.markdown("#### Aggregate report")
        # `st.markdown`, not `st.code`: `format_report` returns Markdown — the same
        # string the `/eval` slash command renders in the chat. Shown as code it was
        # the pipe-and-dash source of a table instead of the table.
        st.markdown(evalhistory.format_report(records, filter=flt or None))

        st.markdown("#### Judged runs")
        st.caption("Zeile anklicken → das aufgehobene Protokoll dieses Laufs erscheint darunter.")
        picked = [
            r
            for r in reversed(records)
            if not flt or flt.lower() in f"{r.get('test_id', '')} {r.get('model', '')}".lower()
        ]
        rows = [
            {
                "ts": r.get("ts"),
                "test": r.get("test_id"),
                "model": r.get("model"),
                "judge": r.get("judge_model"),
                "passed": r.get("passed"),
                "min": (
                    round(r["duration_s"] / 60, 1) if r.get("duration_s") is not None else None
                ),
                "coverage": r.get("tool_coverage"),
                "log": "📄" if log_for(RUNS_DIR, r) else "",
                "reason": (r.get("reason") or "")[:80],
            }
            for r in picked
        ]
        # Row selection rather than a button per row: a button column would rerun the
        # whole script per row and Streamlit has no per-row callback — this is one
        # widget, and the 📄 column says up front which runs have a protocol at all.
        event = st.dataframe(
            rows,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="hist_table",
        )
        # `event.selection` exists at runtime; Streamlit's `DataframeState` stub
        # does not declare it, so the attribute is read through an untyped alias
        # rather than silenced twice at the point of use.
        selection: Any = getattr(event, "selection", None) if event else None
        chosen = ((selection or {}).get("rows") or [None])[0]
        selected_run = picked[chosen] if chosen is not None and chosen < len(picked) else None

        # Not every run is judged, and only judged runs reach the history — the
        # protocol directory is the complete record, so it gets a picker of its own.
        # Both pickers stay together above the protocol: rendered in between, the
        # second one sat a thousand rows below the first.
        logs = run_logs(RUNS_DIR)
        names = {p: p.stem.replace("__", "  ·  ") for p in logs}
        log_pick = st.selectbox(
            f"Alle Protokolle — {len(logs)} Läufe, neueste zuerst",
            logs,
            format_func=lambda p: names[p],
            index=None,
            placeholder="Lauf wählen… (auch ungenotete)",
            key="log_pick",
        )

        path = log_pick or (log_for(RUNS_DIR, selected_run) if selected_run else None)
        if path:
            st.markdown(f"#### Protokoll — `{path.stem}`")
            render_past_run(path)
        elif selected_run:
            st.info(
                "Für diesen Lauf wurde kein Protokoll aufgehoben — die Ablage unter "
                "`.chester/evals/runs/` gibt es erst seit dem 2026-08-16."
            )
