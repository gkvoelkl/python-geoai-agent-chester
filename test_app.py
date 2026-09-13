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
- **Test-Level 2** — the micro-geo probes (`agent-probe-tasks.jsonl`): read and edit
               them, run one or all against the live agent, and read the archived
               results. Same runner as `probe.py`, only presented.
- **Test-Level 4** — the multi-turn dialogues (`agent-dialog-tests.jsonl`): the turns
               run in ONE session, so memory and reference can be tested at all.
               Same machinery as `dialog.py`.

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
from typing import Any, TypedDict

import streamlit as st

# All the heavy lifting is imported from the existing CLI runners — the UI is a
# thin skin so the bench can never drift from `testprompt.py` / `evals.py`.
from ask import ask
from benchlive import log_for, render_past_run, run_logs
from benchview import live_sink, show_artifacts, show_map
from chester import evalhistory
from chester.dialogs import KINDS as DIALOG_KINDS
from chester.dialogs import evaluate as evaluate_dialog
from chester.dialogs import read_history as read_dialog_history
from chester.dialogs import validate as validate_dialog
from chester.evalcells import CELL_ENV, cell_label
from chester.probes import KINDS as PROBE_KINDS
from chester.probes import latest_per_probe, read_history
from dialog import DEFAULT_TIMEOUT_S as DIALOG_TIMEOUT_S
from dialog import archive as archive_dialog
from dialog import load_dialogs, save_dialogs
from dialog import run_turn as run_dialog_turn
from frontier import (
    bare_log_path,
    comparison_record,
    frontier_model_name,
    judge_bare_run,
    read_comparisons,
    record_comparison,
)
from probe import DEFAULT_TIMEOUT_S
from probe import load_tasks as load_probes
from probe import run_task as run_probe_task
from probe import save_tasks as save_probes
from probe import workspace as probe_workspace
from testprompt import (
    CONFIG_NAME,
    PROMPTS_PATH,
    RUNS_DIR,
    STATE_DIR,
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
    pick_stalest_test,
    read_trace,
    run_html,
    save_run_log,
    scoping_notes,
    timestamped_sink,
    validation_note,
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
    # Nur bei einem Panel gefüllt: wer wie gestimmt hat, und ob es einstimmig war.
    panel: dict[str, Any] | None


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

#: Zeitdeckel für die nackte Gegenprobe. Grosszügig gegenüber einem Netzaufruf und
#: trotzdem klein: Ohne Werkzeuge gibt es keine Werkzeugkette, ein Aufruf genügt.
BARE_TIMEOUT_S = 300


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
    """The gateway's agent (same wiring as testprompt), built once.

    ``load_dotenv`` before the build, not after: with a hosted ``model.model``
    (cell F+) the provider reads ``ANTHROPIC_API_KEY`` while the model object is
    constructed, and this runner is the one that never called it — the other three
    do it in their ``main()``, which Streamlit never reaches. Without this line the
    bench builds a keyless client and the first run of a measuring night dies on
    authentication.
    """
    from dotenv import load_dotenv

    from setup import setup

    load_dotenv()
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


def stream_agent(agent, prompt: str, session_key: str, placeholder) -> str:
    """Run one prompt, drawing the timestamped protocol **live** into ``placeholder``.

    Ein Strom, ein Format: Derselbe ``sink``, den `ask.py` im Terminal benutzt,
    schreibt hier in die Oberfläche und wird als Laufprotokoll aufgehoben. Die
    frühere zusammengeführte Zeitleiste (SelmaKits Transcript-Ansicht daneben) ist
    mit SelmaKit 0.1.33 entfallen und **nicht** nachgebaut worden — beim Nachlesen
    zählte immer das Protokoll (`benchlive.py`, Modul-Docstring).

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
        # Vorgabe ist das volle Panel: Es liefert das genauere Urteil, weil die
        # Fehler eines einzelnen Judges Schlagseite sind und keine Streuung —
        # Wiederholung mittelt die nicht weg, verschiedene Herkunftslinien schon.
        # Der Schalter kostet Genauigkeit und spart Zeit: gemessen 2,8 min gegen
        # 17,3 min je Lauf, weil drei ~19-GB-Modelle nacheinander geladen werden.
        single_judge = c3.toggle(
            "nur 1 Judge",
            value=False,
            help="Vorgabe: alle Judges aus evals.judge_models (genauer). "
                 "Eingeschaltet benotet nur der erste — rund 6x schneller, "
                 "aber ohne Mehrheit und ohne die geteilten Urteile.",
        )
        judge_model = c4.text_input("Judge model override", placeholder="evals.judge_models")

        # ── Gegenprobe: derselbe Fall an ein nacktes Frontier-Modell ──
        # Die Kompensationsfrage (doc/tool-compensation.md) an einem einzelnen Fall:
        # gleicher Prompt, gleiche Rubrik, gleicher Judge — nur ohne Werkzeugkasten.
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

                # Erst jetzt, und nur mit einem Chester-Urteil in der Hand: sonst
                # stünde eine Note ohne Gegenstück da.
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

        # ── Die Gegenprobe, sobald eine vorliegt ──
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
                    # Tokenverbrauch nur bei der bezahlten Zelle — die Grundlage der
                    # Kostenschätzung, die Phase KO vor den Messläufen verlangt.
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
                    # Der Weg zur vollen Spur — die Antwort hier ist das Ergebnis,
                    # das Log ist der Hergang (Denken, Abbruchgrund, Judge-Dauer).
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
                      "Frontier": c["frontier"].get("model"),
                      "Judge Chester": c["chester"].get("passed"),
                      "Judge Frontier": c["frontier"].get("passed"),
                      "dein Urteil": (c.get("human") or {}).get("verdict", "—")}
                     for c in reversed(cmps)],
                    width="stretch", hide_index=True, key="frontier_hist",
                )


# ── Edit / New ───────────────────────────────────────────────────────────────
with tab_edit:
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


# ── Test-Level 2 — Mikro-Geo-Proben ──────────────────────────────────────────
# Dieselbe Maschinerie wie `probe.py`, nur dargestellt: gefahren wird mit
# `run_probe_task`, geprüft mit `chester.probes`, archiviert in dieselbe Historie.
# Anders als die Bank braucht diese Stufe keinen Judge — gemessen wird am
# erzeugten Artefakt (`doc/test-levels.md`).
with tab_probe:
    probes = load_probes()
    hist = read_history()
    latest = latest_per_probe(hist)

    st.caption(
        f"Datei: `agent-probe-tasks.jsonl` — {len(probes)} Proben · "
        f"kein Judge, kein Netz · Zeitdeckel je Probe"
    )

    left, right = st.columns([2, 1])
    with left:
        ids = [t["id"] for t in probes]
        marks = {
            i: ("✅" if latest.get(i, {}).get("passed") else ("❌" if i in latest else "·"))
            for i in ids
        }
        pick = st.selectbox(
            "Probe", sorted(ids), format_func=lambda i: f"{marks[i]} {i}", key="probe_pick"
        )
        task = next(t for t in probes if t["id"] == pick)
    with right:
        timeout_s = st.number_input(
            "Zeitdeckel (s)", min_value=30, max_value=1800, value=int(DEFAULT_TIMEOUT_S), step=30,
            help="Wer ihn reißt, ist durchgefallen — ohne Deckel kreiste eine Probe elf Stunden.",
        )
        run_one = st.button("▶ Diese Probe", type="primary", key="probe_run_one")
        run_all_btn = st.button("▶▶ Alle Proben", key="probe_run_all")

    st.markdown(f"**Falle:** {task.get('trap', '—')}")
    st.code(task["prompt_de"], language=None)

    with st.expander("✎ Bearbeiten"):
        with st.form("probe_form"):
            c1, c2 = st.columns(2)
            p_id = c1.text_input("id", value=task["id"])
            p_op = c2.text_input("operation", value=task.get("operation", ""))
            p_trap = st.text_area("trap", value=task.get("trap", ""), height=68)
            p_prompt = st.text_area("prompt_de", value=task["prompt_de"], height=100)
            p_fix = st.text_input(
                "fixtures (Komma-getrennt, aus samples/probe/)",
                value=", ".join(task.get("fixtures", [])),
            )
            p_asserts = st.text_area(
                f"assertions (JSON-Liste; Prüfarten: {', '.join(PROBE_KINDS)})",
                value=json.dumps(task.get("assertions", []), ensure_ascii=False, indent=2),
                height=220,
            )
            saved = st.form_submit_button("💾 Speichern", type="primary")
        if saved:
            try:
                parsed = json.loads(p_asserts)
                bad = [a.get("kind") for a in parsed if a.get("kind") not in PROBE_KINDS]
                if not isinstance(parsed, list) or bad:
                    raise ValueError(f"unbekannte Prüfart(en): {bad}")
            except (ValueError, AttributeError) as exc:
                st.error(f"assertions sind kein gültiger Prüfsatz: {exc}")
            else:
                record = {
                    "id": p_id.strip(), "operation": p_op.strip(), "trap": p_trap.strip(),
                    "prompt_de": p_prompt.strip(),
                    "fixtures": [f.strip() for f in p_fix.split(",") if f.strip()],
                    "assertions": parsed,
                }
                save_probes([record if t["id"] == pick else t for t in probes])
                st.success(f"`{record['id']}` gespeichert.")
                st.rerun()

    if run_one or run_all_btn:
        todo = probes if run_all_btn else [task]
        agent = get_agent()
        ws = probe_workspace()
        progress = st.empty()
        passed_n = 0
        for i, t in enumerate(todo, 1):
            progress.info(f"[{i}/{len(todo)}] {t['id']} läuft … (Deckel {timeout_s}s)")
            stream = st.empty()
            buf: list[str] = []

            def sink(chunk: str, _buf: list[str] = buf, _slot=stream) -> None:
                _buf.append(chunk)
                _slot.code("".join(_buf)[-4000:], language=None)

            ok, secs, lines = run_coro(
                run_probe_task(agent, t, ws, False, float(timeout_s), sink)
            )
            passed_n += ok
            stream.empty()
            with st.container(border=True):
                mark = "✅" if ok else "❌"
                st.markdown(f"{mark} **{t['id']}** · {secs:.0f}s · {t['operation']}")
                for line in lines:
                    st.markdown(line.replace("  ", "", 1))
                produced = [str(ws / a["path"]) for a in t["assertions"]
                            if a.get("path") and (ws / a["path"]).exists()]
                show_artifacts(produced, key=f"probe_{i}")

        progress.success(f"{passed_n}/{len(todo)} bestanden")

    st.markdown("#### Bisherige Ergebnisse")
    if not hist:
        st.info("Noch keine Läufe archiviert — eine Probe starten füllt die Historie.")
    else:
        st.dataframe(
            [
                {
                    "ts": r.get("ts", "")[:16].replace("T", " "),
                    "Probe": r.get("id"),
                    "Operation": r.get("operation"),
                    "Modell": r.get("model"),
                    "bestanden": r.get("passed"),
                    "Deckel gerissen": r.get("timed_out"),
                    "s": r.get("duration_s"),
                    "Prüfungen": " · ".join(c.strip() for c in r.get("checks", []))[:90],
                }
                for r in reversed(hist)
            ],
            width="stretch",
            hide_index=True,
            key="probe_hist",
        )


# ── Test-Level 4 — Dialoge ───────────────────────────────────────────────────
# Dieselbe Maschinerie wie `dialog.py`: gefahren wird Schritt für Schritt mit
# `run_dialog_turn` in **einer** Sitzung, geprüft mit `chester/dialogs.py`,
# archiviert über `archive_dialog`. Über bestanden entscheiden die maschinellen
# Prüfungen; die Auslegungsfragen stehen unbewertet daneben (`doc/test-levels.md`).
with tab_dialog:
    dialogs = load_dialogs()
    dhist = read_dialog_history()
    dlatest: dict[str, dict] = {}
    for row in dhist:
        dlatest[row.get("id", "")] = row

    st.caption(
        f"Datei: `agent-dialog-tests.jsonl` — {len(dialogs)} Dialog(e) · "
        "die Schritte laufen in EINER Sitzung, sonst wäre Gedächtnis nicht prüfbar"
    )
    dleft, dright = st.columns([2, 1])
    with dleft:
        dids = [d["id"] for d in dialogs]
        dmarks = {
            i: ("✅" if dlatest.get(i, {}).get("passed") else ("❌" if i in dlatest else "·"))
            for i in dids
        }
        dpick = st.selectbox(
            "Dialog", sorted(dids), format_func=lambda i: f"{dmarks[i]} {i}", key="dialog_pick"
        )
        dialog = next(d for d in dialogs if d["id"] == dpick)
    with dright:
        dtimeout = st.number_input(
            "Zeitdeckel je Schritt (s)", min_value=60, max_value=3600,
            value=int(DIALOG_TIMEOUT_S), step=60,
            help="Ein Dialogschritt ist eine ganze Aufgabe, kein Einzelschritt.",
        )
        fresh_dialog = st.checkbox(
            "GeoCache vorher leeren", key="dialog_fresh",
            help="Ohne das arbeitet der Dialog auf den Dateien früherer Läufe weiter — "
                 "ein Schritt kann dann auf einer Ebene bestehen, die er nie erzeugt hat.",
        )
        run_dialog_btn = st.button("▶ Dialog fahren", type="primary", key="dialog_run")

    st.markdown(f"**{dialog['category']}** — {dialog.get('origin', '')}")
    for i, spec in enumerate(dialog["turns"], 1):
        with st.container(border=True):
            st.markdown(f"**Schritt {i}**")
            st.code(spec["prompt_de"], language=None)
            for c in spec.get("criteria", []):
                st.markdown(f"- {c}")

    with st.expander("✎ Bearbeiten"):
        # Eigener Auswahlkasten statt Bindung an den Runner oben: Der Editor stand
        # sonst still an `dpick` — man sah nicht, welcher Fall bearbeitet wird, und
        # neue Dialoge liessen sich gar nicht anlegen (2026-09-05 gemeldet).
        e_pick = st.selectbox(
            "Welchen Dialog bearbeiten?", ["➕ Neuer Dialog", *sorted(dids)],
            index=(sorted(dids).index(dpick) + 1) if dpick in dids else 0,
            key="dialog_edit_pick",
        )
        is_new = e_pick == "➕ Neuer Dialog"
        src = {"turns": []} if is_new else next(d for d in dialogs if d["id"] == e_pick)
        # Der Schluesselsuffix ist der Punkt: Streamlit behaelt den Zustand je
        # Widget-Schluessel und ignoriert `value` ab dem zweiten Rendern. Ohne den
        # Suffix zeigte der Editor beim Umschalten weiter den vorigen Fall — genau
        # der gemeldete Fehler.
        k = e_pick.replace(" ", "_")
        d_n = st.number_input(
            "Schritte", min_value=2, max_value=8, step=1,
            value=max(2, len(src.get("turns") or [])),
            key=f"dlg_n_{k}",
            help="Ein Dialog mit einem Schritt ist ein Prompt, kein Dialog.",
        )
        with st.form(f"dialog_form_{k}"):
            c1, c2 = st.columns(2)
            d_id = c1.text_input("id", value=src.get("id", ""), key=f"dlg_id_{k}")
            d_cat = c2.text_input(
                "category", value=src.get("category", "D3. Incremental Refinement"),
                key=f"dlg_cat_{k}",
            )
            d_origin = st.text_input(
                "origin — woher der Fall stammt", value=src.get("origin", ""),
                key=f"dlg_org_{k}",
            )
            # Frueher stand hier eine JSON-Liste. Der Prompt ist das Feld, das man
            # beim Bauen eines Dialogfalls am haeufigsten anfasst — er gehoert nicht
            # in einen Klumpen, in dem ein fehlendes Komma die Eingabe verwirft.
            d_turns_in = []
            turns_src = src.get("turns") or []
            for i in range(int(d_n)):
                src_t = turns_src[i] if i < len(turns_src) else {}
                st.markdown(f"**Schritt {i + 1}**")
                d_turns_in.append((
                    st.text_area(
                        f"prompt_de {i + 1}", value=src_t.get("prompt_de", ""),
                        height=68, key=f"dlg_prompt_{k}_{i}",
                    ),
                    st.text_area(
                        f"criteria {i + 1} (eine je Zeile)",
                        value="\n".join(src_t.get("criteria", [])),
                        height=90, key=f"dlg_crit_{k}_{i}",
                    ),
                ))
            d_checks = st.text_area(
                f"checks (JSON-Liste; Prüfarten: {', '.join(DIALOG_KINDS)})",
                value=json.dumps(src.get("checks", []), ensure_ascii=False, indent=2),
                height=200, key=f"dlg_checks_{k}",
            )
            d_judge = st.text_area(
                "judge_criteria (Auslegung, eine je Zeile — bleibt unbewertet)",
                value="\n".join(src.get("judge_criteria", [])), height=90,
                key=f"dlg_judge_{k}",
            )
            d_saved = st.form_submit_button("💾 Speichern", type="primary")
        if d_saved:
            try:
                checks_parsed = json.loads(d_checks)
            except ValueError as exc:
                st.error(f"checks ist kein gültiges JSON: {exc}")
            else:
                record = {
                    "id": d_id.strip(), "category": d_cat.strip(),
                    "origin": d_origin.strip(),
                    "turns": [
                        {"prompt_de": pr.strip(),
                         "criteria": [c.strip() for c in cr.splitlines() if c.strip()]}
                        for pr, cr in d_turns_in
                    ],
                    "checks": checks_parsed,
                    "judge_criteria": [c.strip() for c in d_judge.splitlines() if c.strip()],
                }
                # `validate` prüft mehr als die frühere Inline-Fassung: fehlende
                # Pflichtfelder je Prüfart, turn-Nummern ausserhalb der Schrittzahl,
                # ungültiges `family`, und `fewer_calls_than` gegen sich selbst.
                problems = validate_dialog(record)
                if problems:
                    st.error("nicht gespeichert — " + " · ".join(problems))
                else:
                    others = [d for d in dialogs if d["id"] not in (record["id"], e_pick)]
                    save_dialogs([*others, record])
                    st.success(f"`{record['id']}` gespeichert ({len(others) + 1} Dialoge).")
                    st.rerun()

        if not is_new and st.checkbox(f"Löschen von `{e_pick}` bestätigen", key=f"dlg_del_{k}"):
            if st.button("🗑 Diesen Dialog löschen", key=f"dlg_delbtn_{k}"):
                save_dialogs([d for d in dialogs if d["id"] != e_pick])
                st.warning(f"`{e_pick}` gelöscht.")
                st.rerun()

    if run_dialog_btn:
        agent = get_agent()
        ws = probe_workspace()
        turns_run = []
        session_key = f"dialog:{dialog['id']}"
        clear_session(session_key)  # ein Dialog beginnt am Anfang
        if fresh_dialog:
            clear_geocache()
            st.caption("GeoCache geleert — der Dialog beginnt auf leerem Arbeitsverzeichnis.")
        for i, spec in enumerate(dialog["turns"], 1):
            st.markdown(f"**Schritt {i}/{len(dialog['turns'])}** · {spec['prompt_de']}")
            slot = st.empty()
            chunks: list[str] = []

            def dsink(chunk: str, _c: list[str] = chunks, _slot=slot) -> None:
                _c.append(chunk)
                _slot.code("".join(_c)[-4000:], language=None)

            turn = run_coro(
                run_dialog_turn(agent, session_key, spec, ws, float(dtimeout), dsink)
            )
            slot.empty()
            turns_run.append(turn)
            st.caption(
                f"{len(turn.tool_calls)} Aufrufe · {turn.duration_s:.0f}s · "
                f"Werkzeuge: {', '.join(turn.tools) or '—'}"
            )
            show_artifacts(turn.written, key=f"dialog_{i}")
            if turn.answer:
                st.markdown(turn.answer[:1500])
            if turn.timed_out:
                # Ein abgebrochener Schritt hinterlässt keine Sitzung; jeder weitere
                # begänne bei null. Weiterlaufen erzeugte Zahlen über einen anderen
                # Gegenstand (`chester/dialogs.py`, `aborted_after`).
                st.warning(
                    f"Zeitdeckel gerissen — Dialog hier beendet, "
                    f"{len(dialog['turns']) - i} Schritt(e) entfallen."
                )
                break
        passed, lines = evaluate_dialog(dialog, turns_run, workspace=ws)
        archive_dialog(dialog, turns_run, passed=passed, lines=lines)
        with st.container(border=True):
            st.markdown(f"{'✅' if passed else '❌'} **maschinelle Prüfungen**")
            for line in lines:
                st.markdown(line.replace("  ", "", 1))
            if dialog.get("judge_criteria"):
                st.caption("offen (Auslegung, nicht bewertet):")
                for c in dialog["judge_criteria"]:
                    st.caption(f"· {c}")

    st.markdown("#### Bisherige Läufe")
    if not dhist:
        st.info("Noch keine Dialogläufe archiviert.")
    else:
        st.dataframe(
            [
                {
                    "ts": r.get("ts", "")[:16].replace("T", " "),
                    "Dialog": r.get("id"),
                    "Kategorie": r.get("category"),
                    "Modell": r.get("model"),
                    "bestanden": r.get("passed"),
                    "Schritte": len(r.get("turns", [])),
                    "Aufrufe": " / ".join(str(len(t.get("tools", []))) for t in r.get("turns", [])),
                    "Prüfungen": " · ".join(c.strip() for c in r.get("checks", []))[:110],
                }
                for r in reversed(dhist)
            ],
            width="stretch", hide_index=True, key="dialog_hist",
        )

