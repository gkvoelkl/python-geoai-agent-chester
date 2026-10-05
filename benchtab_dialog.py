"""Test-Level 4 — the dialogues, turn by turn.

One tab of the test bench (`test_app.py`), split out on 2026-10-05; the body is the
former `with tab_dialog:` block, unchanged.
"""

from __future__ import annotations

import json

import streamlit as st

# All the heavy lifting is imported from the existing CLI runners — the UI is a
# thin skin so the bench can never drift from `testprompt.py` / `evals.py`.
from benchshared import (
    _mine,
    get_agent,
    run_coro,
)
from benchview import show_artifacts
from chester.dialogs import KINDS as DIALOG_KINDS
from chester.dialogs import evaluate as evaluate_dialog
from chester.dialogs import read_history as read_dialog_history
from chester.dialogs import validate as validate_dialog
from dialog import DEFAULT_TIMEOUT_S as DIALOG_TIMEOUT_S
from dialog import archive as archive_dialog
from dialog import load_dialogs, save_dialogs
from dialog import run_turn as run_dialog_turn
from probe import workspace as probe_workspace
from testprompt import (
    clear_geocache,
    clear_session,
)


def render() -> None:  # noqa: C901, PLR0915  # Streamlit tab: one branch per widget
    dialogs = load_dialogs()
    dhist = _mine(read_dialog_history())
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
        # Its own select box instead of binding to the runner above: otherwise the
        # editor was silently tied to `dpick` — one could not see which case was being
        # edited, and new dialogues could not be created at all (reported 2026-09-05).
        e_pick = st.selectbox(
            "Welchen Dialog bearbeiten?", ["➕ Neuer Dialog", *sorted(dids)],
            index=(sorted(dids).index(dpick) + 1) if dpick in dids else 0,
            key="dialog_edit_pick",
        )
        is_new = e_pick == "➕ Neuer Dialog"
        src = {"turns": []} if is_new else next(d for d in dialogs if d["id"] == e_pick)
        # The key suffix is the point: Streamlit keeps state per widget key and ignores
        # `value` from the second render on. Without the suffix the editor kept showing
        # the previous case after switching — exactly the reported defect.
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
            # This used to be a JSON list. The prompt is the field one touches most when
            # building a dialogue case — it does not belong in a lump where one missing
            # comma throws the input away.
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
                # Required fields per check kind, turn numbers beyond the step count,
                # an invalid `family`, and `fewer_calls_than` against itself.
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
                # An aborted step leaves no session; every further one would start from
                # zero. Carrying on would produce numbers about a different subject
                # (`chester/dialogs.py`, `aborted_after`).
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
