"""Run — one test from the bank, judged and archived, plus the frontier comparison.

One tab of the test bench (`test_app.py`), split out on 2026-10-05; the body is the
former `with tab_run:` block, unchanged.
"""

from __future__ import annotations

import contextlib
import io
import time
import warnings

import streamlit as st

import benchtab_frontier
from ask import ask

# All the heavy lifting is imported from the existing CLI runners — the UI is a
# thin skin so the bench can never drift from `testprompt.py` / `evals.py`.
from benchshared import (
    BARE_TIMEOUT_S,
    RunResult,
    _age_label,
    get_agent,
    pick_random_test,
    pick_stalest,
    run_coro,
)
from benchview import live_sink, show_map
from frontier import (
    bare_log_path,
    comparison_record,
    frontier_model_name,
    judge_bare_run,
)
from testprompt import (
    TraceUnavailable,
    archive_run,
    build_judge_panel,
    clear_geocache,
    clear_session,
    config_model_name,
    judge_panel_run,
    last_used,
    layer_facts,
    load_tests,
    read_trace,
    run_html,
    save_run_log,
    scoping_notes,
    timestamped_sink,
    validation_note,
)


def stream_agent(agent, prompt: str, session_key: str, placeholder) -> str:
    """Run one prompt, drawing the timestamped protocol **live** into ``placeholder``.

    One stream, one format: the same ``sink`` `ask.py` uses in the terminal writes into
    the UI here and is kept as the run's protocol. The former merged timeline (SelmaKit's
    transcript view beside it) went away with SelmaKit 0.1.33 and was **not** rebuilt —
    on rereading, the protocol was always what counted (`benchlive.py`, module docstring).

    Library warnings (pyogrio/GDAL) are silenced so they don't clutter the log.
    """
    chunks: list[str] = []

    def sink(chunk: str) -> None:
        chunks.append(chunk)
        placeholder.code("".join(chunks)[-6000:], language=None)

    timed = timestamped_sink(sink)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        final_answer = run_coro(
            ask(agent, prompt, session_key=session_key, show_tools=True, sink=timed)
        )
    # Same reason as in `testprompt.py` and `evals.py`, and the third runner that
    # needed it: the gate's advisory tier is appended to the **returned** answer, not
    # to the stream — so a runner that keeps only the stream loses it, and with it the
    # judge that reads the protocol. Found 2026-09-01 on
    # `pluvial-flow-accumulation-tegernheim`: the same defect (a placeholder instead of
    # the map path) was flagged in the CLI run and silently absent from the bench run.
    note = validation_note(final_answer)
    if note:
        sink(f"\n[gate] {note}\n")
    return "".join(chunks)


def render() -> None:  # noqa: C901, PLR0915  # Streamlit tab: one branch per widget
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
            "Test", sorted(by_id), format_func=lambda i: labels[i], key="run_pick"
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

        c1, c2, c3, c4 = st.columns([2, 2, 2, 3])
        fresh = c1.toggle("Fresh (clear cache+session)", value=True)
        do_judge = c2.toggle("Judge the run", value=False)
        # The full panel is the default: it gives the more accurate verdict, because
        # a single judge's errors are a bias, not scatter — repetition does not average
        # them out, different lineages do. The switch trades accuracy for time:
        # measured 2.8 min against 17.3 min per run, three ~19 GB models loaded in turn.
        single_judge = c3.toggle(
            "nur 1 Judge",
            value=False,
            help="Vorgabe: alle Judges aus evals.judge_models (genauer). "
                 "Eingeschaltet benotet nur der erste — rund 6x schneller, "
                 "aber ohne Mehrheit und ohne die geteilten Urteile.",
        )
        judge_model = c4.text_input("Judge model override", placeholder="evals.judge_models")

        # ── Gegenprobe: derselbe Fall an ein nacktes Frontier-Modell ──
        # The compensation question (doc/tool-compensation.md) on a single case:
        # same prompt, same rubric, same judge — only without the toolbox.
        fmodel = frontier_model_name()
        with_frontier = st.toggle(
            "🛰 Gegenprobe: Frontier-Modell ohne jedes Werkzeug",
            value=False,
            key="run_frontier",
            help="Nach dem Chester-Lauf geht derselbe Prompt an ein gehostetes Modell "
                 "ohne Werkzeuge, ohne Chester-Instruktion, ohne Sitzung. Derselbe "
                 "Judge benotet beide gegen dieselben success_criteria.",
        )
        if with_frontier:
            if not fmodel:
                st.error(
                    "Kein `evals.frontier_model` in der Config. Eintragen (z. B. "
                    "`\"claude-opus-4-8\"` oder `\"claude-sonnet-5\"`) und "
                    "`ANTHROPIC_API_KEY` in `.env` hinterlegen — sonst läuft nur die "
                    "Chester-Seite."
                )
            elif not do_judge:
                st.warning(
                    "**„Judge the run\" mitschalten.** Ohne das Urteil über den "
                    "Chester-Lauf gibt es nichts zu vergleichen — die Gegenprobe "
                    "liefert sonst nur eine einzelne Note."
                )
            else:
                st.caption(
                    f"Gegenprobe mit `{fmodel}` · **Die Bank läuft live** — ohne "
                    "Werkzeuge kommt das Modell an keine Daten. Benotet wird, ob es "
                    "das Verfahren kennt, nicht ob es die Aufgabe ausführt."
                )

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
                    judge = build_judge_panel(judge_model.strip() or None,
                                             single=single_judge)
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
                trace = stream_agent(get_agent(), prompt, session_key, box)
            duration_s = time.monotonic() - started
            box.empty()  # dasselbe Protokoll steht unten, aufklappbar
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
            }

            if judge is not None and trace_error is None:
                judge_members, judge_name, model_under_test, self_grading = judge
                with st.spinner(f"Judging with {judge_name}…"):
                    judge_started = time.monotonic()
                    try:
                        verdict, coverage, missing, effort, agreement = run_coro(
                            judge_panel_run(judge_members, test, prompt, tools, answer,
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
                            agreement=agreement,
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
                            "panel": agreement,
                        }
                    except Exception as exc:  # noqa: BLE001 - judge must not crash the UI
                        result["judge_error"] = f"{type(exc).__name__}: {exc}"

                # Only now, and only with a Chester verdict in hand: otherwise a grade
                # would stand there without its counterpart.
                if with_frontier and fmodel and result["verdict"]:
                    st.markdown("#### Gegenprobe — live")
                    fbox = st.empty()
                    flog = bare_log_path(test["id"])
                    st.caption(f"Live-Protokoll: `{flog}`")
                    with st.spinner(f"Gegenprobe mit {fmodel} …"):
                        try:
                            bare = run_coro(judge_bare_run(
                                judge_members, test, prompt, fmodel, float(BARE_TIMEOUT_S),
                                sink=live_sink(fbox), log_path=flog))
                            chester_cell = {**result["verdict"],
                                            "model": model_under_test,
                                            "answer": result["answer"],
                                            "duration_s": duration_s}
                            st.session_state["frontier_cmp"] = comparison_record(
                                test, prompt, judge_name, chester_cell, bare)
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Gegenprobe fehlgeschlagen: "
                                     f"{type(exc).__name__}: {exc}")

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
            with st.expander("Protokoll — der Lauf, wie ihn das Terminal druckt",
                             expanded=True):
                if result.get("log_path"):
                    st.caption(f"aufgehoben unter `{result['log_path']}`")
                st.code(result["trace"] or "(no trace)")
            if result["map"]:
                show_map(result["map"])

        benchtab_frontier.render_comparison()
