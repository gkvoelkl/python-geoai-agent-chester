"""Test-Level 2 — the micro-geo probes, run and inspected.

One tab of the test bench (`test_app.py`), split out on 2026-10-05; the body is the
former `with tab_probe:` block, unchanged.
"""

from __future__ import annotations

import json

import streamlit as st

# All the heavy lifting is imported from the existing CLI runners — the UI is a
# thin skin so the bench can never drift from `testprompt.py` / `evals.py`.
from benchshared import (
    AGENT_KIND,
    _mine,
    get_agent,
    run_coro,
)
from benchview import show_artifacts
from chester.probes import KINDS as PROBE_KINDS
from chester.probes import latest_per_probe, read_history
from probe import DEFAULT_TIMEOUT_S, TEAM_TIMEOUT_S
from probe import load_tasks as load_probes
from probe import run_task as run_probe_task
from probe import save_tasks as save_probes
from probe import workspace as probe_workspace


def render() -> None:  # noqa: C901, PLR0915  # Streamlit tab: one branch per widget
    probes = load_probes()
    hist = _mine(read_history())
    latest = latest_per_probe(hist)

    st.caption(
        f"Datei: `probes/agent-probe-tasks.jsonl` — {len(probes)} Proben · "
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
            "Zeitdeckel (s)", min_value=30, max_value=1800, step=30,
            value=int(TEAM_TIMEOUT_S if AGENT_KIND == "team" else DEFAULT_TIMEOUT_S),
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
                "fixtures (Komma-getrennt, aus probes/fixtures/)",
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
