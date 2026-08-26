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
from typing import Any, TypedDict

import streamlit as st

# All the heavy lifting is imported from the existing CLI runners — the UI is a
# thin skin so the bench can never drift from `testprompt.py` / `evals.py`.
from ask import ask
from benchlive import LiveRun, log_for, merged, render, render_past_run, run_logs
from chester import evalhistory
from testprompt import (
    CONFIG_NAME,
    PROMPTS_PATH,
    RUNS_DIR,
    SESSIONS_DIR,
    STATE_DIR,
    TraceUnavailable,
    archive_run,
    build_judge,
    clear_geocache,
    clear_session,
    config_model_name,
    judge_run,
    last_used,
    layer_facts,
    load_tests,
    pick_stalest_test,
    read_trace,
    run_html,
    save_run_log,
    scoping_notes,
    timestamped_sink,
)


class RunVerdict(TypedDict):
    """The judge's grade of one run, as the UI needs it."""

    passed: bool
    reason: str
    coverage: float | None
    missing: list[str] | None
    effort: dict[str, Any] | None
    criteria: list[tuple[str, bool]]
    judge: str
    self_grading: bool


class RunResult(TypedDict):
    """One finished bench run, handed to the render block through `session_state`.

    Typed because the round trip through `st.session_state` erases it: everything
    that comes back out is `Any`, so `result["verdict"]["passed"]` type-checked as
    indexing an unknown — 38 of the project's mypy findings sat in this one block,
    all downstream of that single lost annotation.
    """

    trace: str
    tools: list[str]
    answer: str
    map: str | None
    verdict: RunVerdict | None
    duration_s: float | None
    log_path: str
    judge_error: str | None
    session_key: str
    rows: list[Any]
    times: list[Any]


# Canonical field order for a test record (matches the hand-written bank).
FIELD_ORDER = [
    "id",
    "category",
    "prompt_de",
    "expected_behavior",
    "success_criteria",
    "required_data",
    "data_mode",
    "study_area",
    "tools_expected",
    "notes",
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

    from agent_build import (
        geo_capabilities,
        register_validation_gate,
        selmakit_capabilities,
    )

    agent = Gateway.from_config(
        STATE_DIR,
        CONFIG_NAME,
        capabilities=selmakit_capabilities,
        extra_capabilities=geo_capabilities(),
    ).agent
    # The gate belongs to the wiring under test (see `testprompt.main`).
    register_validation_gate(agent)
    return agent


def run_coro(coro):
    return get_loop().run_until_complete(coro)


def stream_agent(agent, prompt: str, session_key: str, placeholder) -> tuple[str, LiveRun]:
    """Run one prompt, drawing the turn **live** into ``placeholder`` as a transcript.

    Two consumers of the one stream, from the same ``ask`` call: ``sink`` builds the
    timestamped text protocol that is kept as the run log (identical to what the
    terminal prints — that is the point of sharing ``ask``), while ``on_event``
    feeds ``benchlive.LiveRun``, which is what the user watches: tool call and its
    result in one row, untruncated behind a click, each row timed.

    Library warnings (pyogrio/GDAL) are silenced so they don't clutter the log.
    """
    chunks: list[str] = []
    live = LiveRun(placeholder)
    live.start(prompt)
    sink = timestamped_sink(chunks.append)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        run_coro(
            ask(
                agent,
                prompt,
                session_key=session_key,
                show_tools=True,
                sink=sink,
                on_event=live.on_event,
            )
        )
    live.paint(force=True)  # flush whatever the throttle held back
    return "".join(chunks), live


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


def pick_stalest() -> None:
    """Select the test that has never run — or, failing that, the stalest one."""
    chosen = pick_stalest_test(load_tests())
    if chosen:
        st.session_state["run_pick"] = chosen


def _age_label(when: float) -> str:
    """``0.0`` → "never", else a coarse age ("4h", "3d") — the ordering, not the date.

    Coarse on purpose: the question this answers is "is this one overdue?", and a
    full timestamp per row would push the id and category out of view.
    """
    if not when:
        return "never"
    hours = (time.time() - when) / 3600
    if hours < 1:
        return "just now"
    return f"{hours:.0f}h" if hours < 48 else f"{hours / 24:.0f}d"


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
        # The age rides in the label so the whole bank is readable at a glance —
        # otherwise "🕐 Stalest" is an opaque jump and a manual pick is uninformed.
        ages = last_used(list(by_id))
        labels = {
            t["id"]: f"{t['id']}  ·  {t.get('category', '-')}  ·  {_age_label(ages[t['id']])}"
            for t in tests
        }
        pcol, rcol, scol = st.columns([5, 1, 1], vertical_alignment="bottom")
        chosen = pcol.selectbox(
            "Test", list(by_id), format_func=lambda i: labels[i], key="run_pick"
        )
        rcol.button(
            "🎲 Random",
            on_click=pick_random_test,
            width="stretch",
            help="Pick a random test from the bank",
        )
        scol.button(
            "🕐 Stalest",
            on_click=pick_stalest,
            width="stretch",
            help="Pick a test that has never run — or, failing that, the one idle longest",
        )
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
                st.markdown(
                    "**Tools expected** — " + ", ".join(f"`{t}`" for t in test["tools_expected"])
                )

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
            box = st.empty()
            started = time.monotonic()
            with st.spinner("Running agent…"):
                trace, live = stream_agent(get_agent(), prompt, session_key, box)
            duration_s = time.monotonic() - started
            box.empty()  # the same rows come back below, with the model's input in front
            # Kept before anything can still fail: the protocol of a run that ended in
            # a judging error is exactly the one worth reading afterwards.
            log_path = save_run_log(
                test["id"],
                config_model_name(),
                session_key,
                trace,
                duration_s=duration_s,
            )
            # An unreadable trace is shown, never judged: the run above may have been
            # perfect, and grading what we failed to read back produces a confident
            # FAIL about nothing. The streamed trace stays visible either way — and
            # doubles as the fallback source when the run died before SelmaKit could
            # persist a session, so a crash is still gradable *as* a crash.
            try:
                tools, answer = read_trace(session_key, trace)
                trace_error = None
            except TraceUnavailable as exc:
                tools, answer = [], ""
                trace_error = str(exc)

            result: RunResult = {
                "trace": trace,
                "tools": tools,
                "answer": answer,
                "map": run_html(session_key),
                "verdict": None,
                "duration_s": duration_s,
                "log_path": str(log_path),
                "judge_error": trace_error,
                "session_key": session_key,
                # Kept, not re-derived: the timings exist only in the stream, and the
                # session file has no timestamp per part to reconstruct them from.
                "rows": live.rows,
                "times": live.times,
            }

            if judge is not None and trace_error is None:
                judge_agent, judge_name, model_under_test, self_grading = judge
                with st.spinner(f"Judging with {judge_name}…"):
                    judge_started = time.monotonic()
                    try:
                        verdict, coverage, missing, effort = run_coro(
                            judge_run(judge_agent, test, prompt, tools, answer,
                                      scope=scoping_notes(session_key),
                                      facts=layer_facts(session_key))
                        )
                        archive_run(
                            test,
                            prompt,
                            "de",
                            model_under_test,
                            judge_name,
                            tools,
                            coverage,
                            verdict,
                            duration_s=duration_s,
                            judge_duration_s=time.monotonic() - judge_started,
                            effort=effort,
                            log=str(log_path),
                        )
                        result["verdict"] = {
                            "passed": verdict.passed,
                            "reason": verdict.reason,
                            "coverage": coverage,
                            "missing": missing,
                            "effort": effort,
                            "criteria": [(c.text, c.passed) for c in verdict.criteria],
                            "judge": judge_name,
                            "self_grading": self_grading,
                        }
                    except Exception as exc:  # noqa: BLE001 - judge must not crash the UI
                        result["judge_error"] = f"{type(exc).__name__}: {exc}"

            st.session_state["run_result"] = result

        # Render the last run (persists across reruns). Re-annotated on the way
        # out: `session_state` hands back `Any`, and every field access below
        # depends on getting the shape back.
        stored: RunResult | None = st.session_state.get("run_result")
        if stored:
            result = stored
            v = result.get("verdict")
            if v:
                head = "✅ PASS" if v["passed"] else "❌ FAIL"
                cov = "–" if v["coverage"] is None else f"{round(v['coverage'] * 100)}%"
                (st.success if v["passed"] else st.error)(f"{head} — {v['reason']}")
                st.caption(
                    f"Judge: {v['judge']}" + ("  ⚠ self-grading" if v["self_grading"] else "")
                )
                for text, ok in v["criteria"]:
                    st.markdown(("✓ " if ok else "✗ ") + text)
                st.markdown(
                    f"**Tool coverage:** {cov}"
                    + (f"  ·  missing: {', '.join(v['missing'])}" if v["missing"] else "")
                )
                eff = v.get("effort")
                if eff:
                    per = "" if eff["per_step"] is None else f"  ·  {eff['per_step']}× the plan"
                    st.markdown(
                        f"**Tool calls:** {eff['calls']} in {eff['distinct']} tool(s){per}"
                        + (f"  ·  off-plan: {', '.join(eff['offplan'])}" if eff["offplan"] else "")
                    )
            elif result.get("judge_error"):
                st.warning(f"[judge] could not grade this run: {result['judge_error']}")

            duration = result["duration_s"]
            if duration is not None:
                st.caption(f"Agent run: {duration / 60:.1f} min ({duration:.0f} s)")
            st.markdown("#### Answer")
            st.markdown(result["answer"] or "_(empty)_")
            st.markdown(
                "**Tools called:** " + (", ".join(f"`{t}`" for t in result["tools"]) or "_none_")
            )
            # One timeline for the whole run: what the model was given (from the
            # session file), what it said and called (as streamed, with timings),
            # tool call and result in one expandable row. Same rows as during the
            # run — the model's input is what the end of the turn adds.
            with st.expander("Run — model input, tool exchange, timings", expanded=True):
                rows, times = merged(
                    str(SESSIONS_DIR),
                    result["session_key"],
                    result.get("rows") or [],
                    result.get("times") or [],
                )
                render(rows, times)
                if not rows:
                    st.caption(
                        "Kein Session-Trace für diesen Lauf — dieselbe Ursache, die "
                        "auch die Benotung verhindert."
                    )
            with st.expander("Raw log — the protocol as the terminal prints it"):
                if result.get("log_path"):
                    st.caption(f"aufgehoben unter `{result['log_path']}`")
                st.code(result["trace"] or "(no trace)")
            map_path = result["map"]
            if map_path:
                with st.expander("Rendered map / 3D view", expanded=True):
                    st.caption(f"`{map_path}`")
                    html = Path(map_path).read_text(encoding="utf-8")
                    if len(html) < 8_000_000:
                        st.iframe(html, height=500)
                    else:
                        st.caption(
                            f"Too large to embed ({len(html) // 1_000_000} MB): {map_path}"
                        )


# ── Edit / New ───────────────────────────────────────────────────────────────
with tab_edit:
    tests = load_tests()
    ids = [t["id"] for t in tests]
    pick = st.selectbox("Edit test", ["➕ New test", *ids], key="edit_pick")
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


# ── History ──────────────────────────────────────────────────────────────────
with tab_hist:
    records = evalhistory.load_history()
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
