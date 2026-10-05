"""What every tab of the test bench shares: run state, agent, event loop, small helpers.

Split out of `test_app.py` on 2026-10-05 (1124 lines) together with one module per
tab (`benchtab_*.py`). Flat in the root on purpose, next to `benchlive`/`benchview`:
the structure guards in `tests/` scan the root's `*.py` — a runner moved into a
subpackage would drop out of "every runner keeps the gate note" without a sound.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, TypedDict

import streamlit as st

# All the heavy lifting is imported from the existing CLI runners — the UI is a
# thin skin so the bench can never drift from `testprompt.py` / `evals.py`.
from chester.evalcells import agent_kind
from testprompt import (
    PROMPTS_PATH,
    load_tests,
    pick_stalest_test,
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
    # Filled only for a panel: who voted how, and whether it was unanimous.
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

#: Time limit for the bare counter-run. Generous for one network call and still small:
#: without tools there is no tool chain, one call is all it makes.
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
    from agents import build_agent

    return build_agent()


def run_coro(coro):
    return get_loop().run_until_complete(coro)




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


# `./test_team.sh` sets CHESTER_AGENT=team: the same bench, run against chester-team.
# Every history view below shows only this agent's runs — an agent ✅ must never read
# as a team result (2026-09-19).
AGENT_KIND = agent_kind()

def _mine(rows: list[dict]) -> list[dict]:
    """Only the runs of the agent under test (records before 2026-09-19 are agent)."""
    return [r for r in rows if r.get("agent", "agent") == AGENT_KIND]

