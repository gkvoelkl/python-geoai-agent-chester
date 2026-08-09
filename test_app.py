"""test_app.py — a Streamlit bench for the prompt test suite.

A comfortable UI over the *same* machinery as ``testprompt.py`` / ``evals.py`` /
``chester.evalhistory`` — no logic is duplicated, only presented:

- **Run**    — pick a test, run it (fresh / language / judge), watch the tool
               exchange + answer, get the judge verdict and tool coverage.
- **Edit**   — edit an existing test or create a new one; writes back to
               ``agent-test-prompts.jsonl``.
- **History** — the aggregate report (pass-rate + coverage per model, latest
               verdict per test) plus the raw judged-run log.

Run with:  ``uv run streamlit run test_app.py``  (default :8501).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import random
import time
import warnings
from pathlib import Path

import streamlit as st

# All the heavy lifting is imported from the existing CLI runners — the UI is a
# thin skin so the bench can never drift from `testprompt.py` / `evals.py`.
from ask import ask
from chester import evalhistory
from testprompt import (
    CONFIG_NAME,
    PROMPTS_PATH,
    STATE_DIR,
    archive_run,
    build_judge,
    clear_geocache,
    clear_session,
    judge_run,
    load_tests,
    read_trace,
    run_html,
)

# Canonical field order for a test record (matches the hand-written bank).
FIELD_ORDER = [
    "id", "category", "prompt_de", "expected_behavior",
    "success_criteria", "required_data", "data_mode", "study_area",
    "tools_expected", "notes",
]
DATA_MODES = ["live", "fixture"]


# ── shared resources (built once, reused across reruns) ──────────────────────


@st.cache_resource
def get_loop() -> asyncio.AbstractEventLoop:
    """One persistent event loop for the whole app session.

    The agent's async model client binds to the loop it first runs on; reusing a
    single loop across runs avoids 'event loop is closed' between test runs.
    """
    return asyncio.new_event_loop()


@st.cache_resource
def get_agent():
    """The gateway's agent (same wiring as testprompt), built once."""
    from setup import setup

    setup(quiet=True)
    from selmakit import Gateway

    from agent_build import geo_capabilities

    return Gateway.from_config(
        STATE_DIR, CONFIG_NAME, extra_capabilities=geo_capabilities()
    ).agent


def run_coro(coro):
    return get_loop().run_until_complete(coro)


def stream_agent(agent, prompt: str, session_key: str, placeholder) -> str:
    """Run one prompt, streaming the tool exchange **live** into ``placeholder``.

    Uses ``ask``'s ``sink`` so the UI shows the same stream the CLI prints — as it
    happens, not after the run. Library warnings (pyogrio/GDAL) are silenced so
    they don't clutter; a light throttle avoids re-rendering the whole log on
    every token.
    """
    chunks: list[str] = []
    counter = {"total": 0, "rendered": 0}

    def sink(chunk: str) -> None:
        chunks.append(chunk)
        counter["total"] += len(chunk)
        # Re-render on a line break or once ~200 chars have accumulated.
        if "\n" in chunk or counter["total"] - counter["rendered"] > 200:
            placeholder.code("".join(chunks))
            counter["rendered"] = counter["total"]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        run_coro(ask(agent, prompt, session_key=session_key, show_tools=True, sink=sink))
    text = "".join(chunks)
    placeholder.code(text or "(no output)")  # flush the tail
    return text


def save_tests(tests: list[dict]) -> None:
    """Rewrite the whole JSONL bank (one ordered record per line)."""
    lines = []
    for t in tests:
        ordered = {k: t[k] for k in FIELD_ORDER if t.get(k) not in (None, "", [])}
        for k, v in t.items():  # keep any non-standard keys at the end
            if k not in ordered and v not in (None, "", []):
                ordered[k] = v
        lines.append(json.dumps(ordered, ensure_ascii=True))
    PROMPTS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def pick_random_test() -> None:
    """Select a random test in the Run tab (button ``on_click`` — runs before the
    selectbox is instantiated, so setting its key is safe)."""
    ids = [t["id"] for t in load_tests()]
    if ids:
        st.session_state["run_pick"] = random.choice(ids)


# ── UI ───────────────────────────────────────────────────────────────────────


st.set_page_config(page_title="Chester Test Bench", page_icon="🧪", layout="wide")
st.title("🧪 Chester — Prompt Test Bench")

with st.sidebar:
    st.caption(f"Bank: `{PROMPTS_PATH.name}`")
    if st.button("↻ Rebuild agent", help="Reload config/model (after a config change)"):
        get_agent.clear()
        st.success("Agent will rebuild on next run.")

tab_run, tab_edit, tab_hist = st.tabs(["▶ Run", "✎ Edit / New", "📊 History"])


# ── Run ──────────────────────────────────────────────────────────────────────
with tab_run:
    tests = load_tests()
    if not tests:
        st.info("No tests in the bank yet — add one in the *Edit / New* tab.")
    else:
        by_id = {t["id"]: t for t in tests}
        labels = {
            t["id"]: f"{t['id']}  ·  {t.get('category', '-')}"
            for t in tests
        }
        pcol, bcol = st.columns([5, 1], vertical_alignment="bottom")
        chosen = pcol.selectbox(
            "Test", list(by_id), format_func=lambda i: labels[i], key="run_pick"
        )
        bcol.button("🎲 Random", on_click=pick_random_test, width="stretch",
                    help="Pick a random test from the bank")
        test = by_id[chosen]

        c1, c2, c3 = st.columns(3)
        fresh = c1.toggle("Fresh (clear cache+session)", value=True)
        do_judge = c2.toggle("Judge the run", value=False)
        judge_model = c3.text_input("Judge model override", placeholder="evals.judge_model")

        prompt = test.get("prompt_de") or ""

        with st.expander("Rubric", expanded=True):
            st.markdown(f"**Prompt** — {prompt}")
            if test.get("expected_behavior"):
                st.markdown(f"**Expected** — {test['expected_behavior']}")
            if test.get("success_criteria"):
                st.markdown("**Success criteria**")
                for c in test["success_criteria"]:
                    st.markdown(f"- {c}")
            if test.get("tools_expected"):
                st.markdown("**Tools expected** — " + ", ".join(f"`{t}`" for t in test["tools_expected"]))

        if st.button("▶ Run test", type="primary"):
            session_key = f"testapp:{test['id']}"
            judge = None
            if do_judge:
                try:
                    judge = build_judge(judge_model.strip() or None)
                except ValueError as exc:
                    st.error(f"Judge not available: {exc}")
                    st.stop()
            if fresh:
                fbuf = io.StringIO()
                with contextlib.redirect_stdout(fbuf):
                    clear_geocache()
                    clear_session(session_key)
                if fbuf.getvalue().strip():
                    st.caption(fbuf.getvalue().strip())

            st.markdown("#### Live run")
            live = st.empty()
            started = time.monotonic()
            with st.spinner("Running agent…"):
                trace = stream_agent(get_agent(), prompt, session_key, live)
            duration_s = time.monotonic() - started
            live.empty()  # replaced by the structured result below
            tools, answer = read_trace(session_key)

            result = {"trace": trace, "tools": tools, "answer": answer,
                      "map": run_html(session_key), "verdict": None,
                      "duration_s": duration_s}

            if judge is not None:
                judge_agent, judge_name, model_under_test, self_grading = judge
                with st.spinner(f"Judging with {judge_name}…"):
                    judge_started = time.monotonic()
                    try:
                        verdict, coverage, missing = run_coro(
                            judge_run(judge_agent, test, prompt, tools, answer)
                        )
                        archive_run(test, prompt, "de", model_under_test,
                                    judge_name, tools, coverage, verdict,
                                    duration_s=duration_s,
                                    judge_duration_s=time.monotonic() - judge_started)
                        result["verdict"] = {
                            "passed": verdict.passed, "reason": verdict.reason,
                            "coverage": coverage, "missing": missing,
                            "criteria": [(c.text, c.passed) for c in verdict.criteria],
                            "judge": judge_name, "self_grading": self_grading,
                        }
                    except Exception as exc:  # noqa: BLE001 - judge must not crash the UI
                        result["judge_error"] = f"{type(exc).__name__}: {exc}"

            st.session_state["run_result"] = result

        # Render the last run (persists across reruns).
        result = st.session_state.get("run_result")
        if result:
            v = result.get("verdict")
            if v:
                head = "✅ PASS" if v["passed"] else "❌ FAIL"
                cov = "–" if v["coverage"] is None else f"{round(v['coverage'] * 100)}%"
                (st.success if v["passed"] else st.error)(f"{head} — {v['reason']}")
                st.caption(f"Judge: {v['judge']}"
                           + ("  ⚠ self-grading" if v["self_grading"] else ""))
                for text, ok in v["criteria"]:
                    st.markdown(("✓ " if ok else "✗ ") + text)
                st.markdown(f"**Tool coverage:** {cov}"
                            + (f"  ·  missing: {', '.join(v['missing'])}" if v["missing"] else ""))
            elif result.get("judge_error"):
                st.warning(f"[judge] could not grade this run: {result['judge_error']}")

            if result.get("duration_s") is not None:
                st.caption(f"Agent run: {result['duration_s'] / 60:.1f} min "
                           f"({result['duration_s']:.0f} s)")
            st.markdown("#### Answer")
            st.markdown(result["answer"] or "_(empty)_")
            st.markdown("**Tools called:** "
                        + (", ".join(f"`{t}`" for t in result["tools"]) or "_none_"))
            with st.expander("Full tool exchange"):
                st.code(result["trace"] or "(no trace)")
            if result.get("map"):
                with st.expander("Rendered map / 3D view", expanded=True):
                    st.caption(f"`{result['map']}`")
                    html = Path(result["map"]).read_text(encoding="utf-8")
                    if len(html) < 8_000_000:
                        st.iframe(html, height=500)
                    else:
                        st.caption(f"Too large to embed ({len(html) // 1_000_000} MB): {result['map']}")


# ── Edit / New ───────────────────────────────────────────────────────────────
with tab_edit:
    tests = load_tests()
    ids = [t["id"] for t in tests]
    pick = st.selectbox("Edit test", ["➕ New test", *ids], key="edit_pick")
    src = {} if pick == "➕ New test" else next(t for t in tests if t["id"] == pick)

    with st.form("edit_form"):
        c1, c2 = st.columns(2)
        f_id = c1.text_input("id", value=src.get("id", ""))
        f_mode = c2.selectbox("data_mode", DATA_MODES,
                              index=DATA_MODES.index(src["data_mode"]) if src.get("data_mode") in DATA_MODES else 0)
        f_cat = st.text_input("category", value=src.get("category", ""))
        f_de = st.text_area("prompt_de", value=src.get("prompt_de", ""), height=68)
        f_exp = st.text_area("expected_behavior", value=src.get("expected_behavior", ""), height=100)
        f_crit = st.text_area("success_criteria (one per line)",
                              value="\n".join(src.get("success_criteria", [])), height=120)
        f_tools = st.text_area("tools_expected (comma or space separated)",
                               value=", ".join(src.get("tools_expected", [])), height=68)
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
                "id": f_id.strip(), "category": f_cat.strip(),
                "prompt_de": f_de.strip(),
                "expected_behavior": f_exp.strip(),
                "success_criteria": [c.strip() for c in f_crit.splitlines() if c.strip()],
                "required_data": f_req.strip(), "data_mode": f_mode,
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


# ── History ──────────────────────────────────────────────────────────────────
with tab_hist:
    records = evalhistory.load_history()
    if not records:
        st.info("No judged runs yet. Run a test with *Judge* enabled to populate the history.")
    else:
        flt = st.text_input("Filter (test id / model substring)", key="hist_filter").strip()
        st.markdown("#### Aggregate report")
        st.code(evalhistory.format_report(records, filter=flt or None))

        st.markdown("#### Judged runs")
        rows = [
            {
                "ts": r.get("ts"), "test": r.get("test_id"),
                "model": r.get("model"), "judge": r.get("judge_model"),
                "passed": r.get("passed"),
                "min": (round(r["duration_s"] / 60, 1)
                        if r.get("duration_s") is not None else None),
                "coverage": r.get("tool_coverage"),
                "reason": (r.get("reason") or "")[:80],
            }
            for r in records
            if not flt or flt.lower() in f"{r.get('test_id','')} {r.get('model','')}".lower()
        ]
        st.dataframe(list(reversed(rows)), width="stretch", hide_index=True)
