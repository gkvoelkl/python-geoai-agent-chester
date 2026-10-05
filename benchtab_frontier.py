"""The frontier counter-run, shown — the same case on a bare frontier model, side by side.

Split out of the run tab (`benchtab_run.py`) on 2026-10-05 to keep both files under the
size limit. It reads only what the run tab left in `st.session_state` and the stored
comparisons; nothing is passed in.
"""

from __future__ import annotations

import streamlit as st

# All the heavy lifting is imported from the existing CLI runners — the UI is a
# thin skin so the bench can never drift from `testprompt.py` / `evals.py`.
from frontier import (
    read_comparisons,
    record_comparison,
)


def render_comparison() -> None:
    # ── The counter-run, once there is one ──
    cmp_row = st.session_state.get("frontier_cmp")
    if cmp_row:
        st.divider()
        st.markdown("#### Gegenprobe — Werkzeugkasten gegen nacktes Frontier-Modell")
        st.caption(
            f"Judge: `{cmp_row['judge_model']}` · dieselbe Rubrik über beide "
            "Zellen. Die nackte Zelle hat keinen Datenzugang — sie zeigt, ob das "
            "Modell das Verfahren kennt, nicht ob es die Aufgabe ausführt."
        )
        cells = (("chester", f"🧭 Chester + Werkzeuge · {cmp_row['chester']['model']}"),
                 ("frontier", f"🛰 {cmp_row['frontier']['model']} · nackt"))
        for col, (key, label) in zip(st.columns(2), cells):
            cell = cmp_row[key]
            with col, st.container(border=True):
                head = {True: "✅ Judge: bestanden",
                        False: "❌ Judge: durchgefallen"}.get(cell["passed"],
                                                             "— nicht benotet")
                secs = cell.get("duration_s") or 0
                st.markdown(f"**{label}**")
                st.markdown(f"{head} · {secs:.0f}s")
                # Token use only for the paid cell — the basis of the cost estimate
                # the measuring runs needed.
                use = cell.get("usage") or {}
                if use:
                    st.caption(
                        f"{use.get('input_tokens', 0):,} in · "
                        f"{use.get('output_tokens', 0):,} out"
                        + (f" · stop: `{cell['stop_reason']}`"
                           if cell.get("stop_reason") not in (None, "end_turn") else "")
                    )
                for c in cell.get("criteria", []):
                    st.markdown(("- ✓ " if c["passed"] else "- ✗ ") + c["text"])
                if cell.get("reason"):
                    st.caption(cell["reason"])
                with st.expander("Antwort im Wortlaut"):
                    st.markdown(cell.get("answer") or "_(leer)_")
                # The way to the full trace — the answer here is the result, the log
                # is how it came about (thinking, reason for stopping, judge time).
                if cell.get("log_path"):
                    st.caption(f"Protokoll: `{cell['log_path']}`")

        st.markdown("**Dein Urteil** — es wird getrennt vom Judge archiviert:")
        with st.form("frontier_verdict"):
            choice = st.radio(
                "Wer löst die Aufgabe besser?",
                ["Chester", "gleichauf", "Frontier", "unentschieden / unklar"],
                horizontal=True,
            )
            note = st.text_area("Begründung (optional)")
            if st.form_submit_button("Urteil festhalten", type="primary"):
                record_comparison({**cmp_row,
                                   "human": {"verdict": choice, "note": note}})
                st.success("Vergleich archiviert.")
                st.session_state.pop("frontier_cmp", None)
                st.rerun()

    cmps = read_comparisons()
    if cmps:
        with st.expander(f"📐 Archivierte Gegenproben ({len(cmps)})"):
            st.dataframe(
                [{"ts": c.get("ts", "")[:16].replace("T", " "),
                  "Test": c.get("test_id"),
                  "Frontier": (c.get("frontier") or {}).get("model"),
                  "Judge Chester": (c.get("chester") or {}).get("passed"),
                  "Judge Frontier": (c.get("frontier") or {}).get("passed"),
                  "dein Urteil": (c.get("human") or {}).get("verdict", "—")}
                 for c in reversed(cmps)],
                width="stretch", hide_index=True, key="frontier_hist",
            )
