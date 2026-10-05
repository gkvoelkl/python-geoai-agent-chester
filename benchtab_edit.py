"""Edit / New — the prompt bank as a form.

One tab of the test bench (`test_app.py`), split out on 2026-10-05; the body is the
former `with tab_edit:` block, unchanged.
"""

from __future__ import annotations

import streamlit as st

# All the heavy lifting is imported from the existing CLI runners — the UI is a
# thin skin so the bench can never drift from `testprompt.py` / `evals.py`.
from benchshared import (
    DATA_MODES,
    save_tests,
)
from testprompt import (
    load_tests,
)


def render() -> None:
    tests = load_tests()
    ids = [t["id"] for t in tests]
    pick = st.selectbox("Edit test", ["➕ New test", *sorted(ids)], key="edit_pick")
    src = {} if pick == "➕ New test" else next(t for t in tests if t["id"] == pick)

    with st.form("edit_form"):
        c1, c2 = st.columns(2)
        f_id = c1.text_input("id", value=src.get("id", ""))
        f_mode = c2.selectbox(
            "data_mode",
            DATA_MODES,
            index=(DATA_MODES.index(src["data_mode"]) if src.get("data_mode") in DATA_MODES else 0),
        )
        f_cat = st.text_input("category", value=src.get("category", ""))
        f_de = st.text_area("prompt_de", value=src.get("prompt_de", ""), height=68)
        f_exp = st.text_area(
            "expected_behavior", value=src.get("expected_behavior", ""), height=100
        )
        f_crit = st.text_area(
            "success_criteria (one per line)",
            value="\n".join(src.get("success_criteria", [])),
            height=120,
        )
        f_tools = st.text_area(
            "tools_expected (comma or space separated)",
            value=", ".join(src.get("tools_expected", [])),
            height=68,
        )
        c4, c5 = st.columns(2)
        f_area = c4.text_input("study_area", value=src.get("study_area", ""))
        f_req = c5.text_input("required_data", value=src.get("required_data", ""))
        f_notes = st.text_area("notes", value=src.get("notes", ""), height=68)

        saved = st.form_submit_button("💾 Save", type="primary")

    if saved:
        if not f_id.strip():
            st.error("id is required.")
        else:
            record = {
                "id": f_id.strip(),
                "category": f_cat.strip(),
                "prompt_de": f_de.strip(),
                "expected_behavior": f_exp.strip(),
                "success_criteria": [c.strip() for c in f_crit.splitlines() if c.strip()],
                "required_data": f_req.strip(),
                "data_mode": f_mode,
                "study_area": f_area.strip(),
                "tools_expected": [x for x in f_tools.replace(",", " ").split() if x],
                "notes": f_notes.strip(),
            }
            others = [t for t in tests if t["id"] != record["id"]]
            save_tests([*others, record])
            st.success(f"Saved `{record['id']}` ({len(others) + 1} tests in the bank).")

    if pick != "➕ New test":
        if st.checkbox(f"Confirm delete `{pick}`"):
            if st.button("🗑 Delete this test"):
                save_tests([t for t in tests if t["id"] != pick])
                st.warning(f"Deleted `{pick}`.")
                st.rerun()
