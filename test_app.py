"""test_app.py — a Streamlit bench for the prompt test suite.

A comfortable UI over the *same* machinery as ``testprompt.py`` / ``evals.py`` /
``chester.evalhistory`` — no logic is duplicated, only presented:

- **Run**    — pick a test, run it (fresh / language / judge), watch the tool
               exchange + answer, get the judge verdict and tool coverage. Optional
               **Gegenprobe**: the same case at a bare frontier model (no tools, no
               Chester instruction), graded by the same judge against the same
               rubric — the compensation question on one case (`frontier.py`).
- **Edit**   — edit an existing test or create a new one; writes back to
               ``agent-test-prompts.jsonl``.
- **History** — the aggregate report (pass-rate + coverage per model, latest
               verdict per test) plus the raw judged-run log.
- **Test-Level 2** — the micro-geo probes (`probes/agent-probe-tasks.jsonl`): read and edit
               them, run one or all against the live agent, and read the archived
               results. Same runner as `probe.py`, only presented.
- **Test-Level 4** — the multi-turn dialogues (`agent-dialog-tests.jsonl`): the turns
               run in ONE session, so memory and reference can be tested at all.
               Same machinery as `dialog.py`.

Run with:  ``uv run streamlit run test_app.py``  (default :8501).
"""

from __future__ import annotations

import streamlit as st

import benchtab_dialog
import benchtab_edit
import benchtab_history
import benchtab_probe
import benchtab_run

# All the heavy lifting is imported from the existing CLI runners — the UI is a
# thin skin so the bench can never drift from `testprompt.py` / `evals.py`.
from benchshared import AGENT_KIND, get_agent
from chester.evalcells import CELL_ENV, cell_label
from testprompt import (
    PROMPTS_PATH,
)

# ── UI ───────────────────────────────────────────────────────────────────────


st.set_page_config(page_title="Chester Test Bench", page_icon="🧪", layout="wide")
st.title("🧪 Chester-Team — Prompt Test Bench" if AGENT_KIND == "team"
         else "🧪 Chester — Prompt Test Bench")


with st.sidebar:
    st.caption(f"Bank: `{PROMPTS_PATH.name}`")
    # The bench archives like the batch does, so the cell question applies here too.
    # The variable comes from the shell that started Streamlit; showing it is what
    # keeps an unlabelled measuring night from surfacing only in the report.
    _cell = cell_label()
    st.caption(f"Messzelle: `{_cell}`" if _cell else f"Messzelle: — (`{CELL_ENV}` nicht gesetzt)")
    if st.button("↻ Rebuild agent", help="Reload config/model (after a config change)"):
        get_agent.clear()
        st.success("Agent will rebuild on next run.")

tab_run, tab_edit, tab_hist, tab_probe, tab_dialog = st.tabs(
    ["▶ Run", "✎ Edit / New", "📊 History", "🔬 Test-Level 2", "💬 Test-Level 4"]
)


# ── Run ──────────────────────────────────────────────────────────────────────
with tab_run:
    benchtab_run.render()


# ── Edit / New ───────────────────────────────────────────────────────────────
with tab_edit:
    benchtab_edit.render()


# ── History ──────────────────────────────────────────────────────────────────
with tab_hist:
    benchtab_history.render()


# ── Test-Level 2 — micro geo probes ──────────────────────────────────────────
# The same machinery as `probe.py`, just displayed: run with `run_probe_task`,
# checked with `chester.probes`, archived into the same history. Unlike the bank
# this level needs no judge — it measures the produced artifact
# (`doc/test-levels.md`).
with tab_probe:
    benchtab_probe.render()


# ── Test-Level 4 — dialogues ─────────────────────────────────────────────────
# The same machinery as `dialog.py`: run step by step with `run_dialog_turn` in
# **one** session, checked with `chester/dialogs.py`, archived via
# `archive_dialog`. The machine checks decide pass; the questions of interpretation
# stand ungraded beside them (`doc/test-levels.md`).
with tab_dialog:
    benchtab_dialog.render()

