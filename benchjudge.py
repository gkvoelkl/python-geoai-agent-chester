"""Grading a bench run: the verdict, the judge panel, and reading what the run left.

Cut out of `testprompt.py` on 2026-10-05, where it had grown to 1287 lines. Kept here
is everything that grades without touching a path or a config: the verdict model,
`judge_run`/`judge_panel_run`, the panel merge, the tool measures and the protocol
readers. Building a judge and finding a session stay in `testprompt.py`, because the
tests patch its `SESSIONS_DIR`/`STATE_DIR` — a function moved here would read its own,
unpatched copy and run against the real directories without anyone noticing.
"""

from __future__ import annotations

import re

from pydantic import AliasChoices, BaseModel, Field

JUDGE_SYSTEM = (
    "You are a strict evaluator for a Geo-AI agent's answer to a benchmark task. "
    "You are given the task, its expected behaviour, its success criteria, plus "
    "what the agent ACTUALLY did: "
    "its tool-call sequence and its final answer. Judge only what the agent "
    "produced against those criteria; do not re-solve the task yourself. Return "
    "one result per success criterion, in the given order, each marked pass/fail. "
    "Judge each criterion against ITS OWN wording and nothing else. The expected "
    "behaviour describes one route that works — it is context, not a requirement. "
    "A different route that meets the criteria passes: an official timetable feed "
    "instead of the OpenStreetMap layer the description names, a shortcut tool "
    "instead of a hand-built chain. Fail a criterion only when that criterion's own "
    "text is unmet, and never move a complaint into a criterion that does not "
    "mention it. "
    "When the same tool was called several times, judge the call **whose result "
    "the run actually used** — normally the last one. An agent that gets a "
    "parameter wrong, is told so, and calls again correctly has met the criterion: "
    "only the final invocation determines the outcome, and self-correction is the "
    "behaviour we want. Wasted attempts are already counted separately as effort, "
    "so do not also charge them here. This does not excuse a wrong *final* state, "
    "nor an answer that reports a result the corrected call never produced. "
    "Set the overall `passed` to true when every criterion that matters is met. "
    "Keep `reason` to one or two sentences."
)


class CriterionResult(BaseModel):
    """One success criterion and whether the agent's answer met it."""

    # Accept "criterion" too — weak judges naturally name the field that way and
    # would otherwise fail validation (observed with a 12B local judge).
    text: str = Field(validation_alias=AliasChoices("text", "criterion"))
    passed: bool


class Verdict(BaseModel):
    """The judge's structured grade of a single run."""

    criteria: list[CriterionResult]
    passed: bool
    reason: str


class TraceUnavailable(RuntimeError):
    """The run cannot be read back — so nothing about it can be graded.

    Raised instead of returning an empty result, because the two are *not* the same
    thing and the difference decides whether a FAIL means anything. Observed
    2026-08-16: a run wrote its Sentinel bands, its NDVI map and even a validation
    snapshot to the GeoCache over 954 s, but no session file appeared. `read_trace`
    reported "no tools, no answer", the judge graded that faithfully, and a FAIL with
    a convincing reason ("the agent produced no tool calls") landed in the history —
    a broken measurement wearing the costume of a finding.
    """


# The protocol lines `ask` emits, behind `timestamped_sink`'s "HH:MM:SS +1.2s │ ".
_PROTOCOL_TOOL_CALL = re.compile(r"│ → (\w+)\(")


_PROTOCOL_RUN_ERROR = re.compile(r"\[run error: (.+?)\]\s*$", re.MULTILINE)


def trace_from_protocol(protocol: str) -> tuple[list[str], str]:
    """Reconstruct tool sequence and outcome from the *streamed* protocol.

    The stand-in for a run that died mid-stream. `ask` catches the exception,
    prints ``[run error: …]`` and returns — but pydantic-ai never emitted an
    ``AgentRunResultEvent``, so SelmaKit's ``_finalize_run`` writes no session at
    all. The tool calls are gone from disk while sitting right there in the text
    the bench just streamed; parsing them back is the difference between grading a
    crash *as* a crash and not grading it.

    The "answer" of an aborted run is its abort reason. Saying that plainly beats
    an empty string, which the judge would have to read as "the model said
    nothing" — the same conflation :class:`TraceUnavailable` exists to prevent.
    """
    tools = _PROTOCOL_TOOL_CALL.findall(protocol or "")
    errors = _PROTOCOL_RUN_ERROR.findall(protocol or "")
    if not errors:
        return tools, ""
    return tools, (
        "(no final answer — the run aborted before it could produce one: "
        + "; ".join(errors)
        + ")"
    )


#: How the gate marks its advisory tier (``chester/gate.py``, end of the validator).
_GATE_MARKER = "🔎 Validation note"


def validation_note(answer: str | None) -> str | None:
    """The gate's advisory note out of a final answer, or ``None``.

    The note lives only in the *returned* string: the stream carries the model's
    text, and SelmaKit persists the pre-validator messages. A runner that logs the
    one and judges the other never sees it — which is how a flagged run looked
    clean for months.
    """
    if not answer or _GATE_MARKER not in answer:
        return None
    return answer.split(_GATE_MARKER, 1)[1].lstrip(" —").strip()


def _merge_verdicts(votes: list[tuple[str, Verdict]]) -> tuple[Verdict, dict]:
    """Majority verdict over the single verdicts, plus how the vote went.

    Majority **per criterion** and **per overall verdict**, counted separately: a judge
    can side with the majority on the criteria and not on the overall verdict — the
    system prompt leaves it room ("every criterion *that matters*"), and exactly this
    aggregation step was the least stable in the measurement of 2026-09-02 (5 % of
    criteria flipped, 20 % of overall verdicts).

    ``agreement`` records who voted how and whether it was unanimous — **a split
    verdict is a finding, not noise**, and belongs in front of a human instead of
    disappearing into a majority.
    """
    passed_votes = [v.passed for _n, v in votes]
    merged_passed = sum(passed_votes) * 2 > len(passed_votes)
    unanimous = len(set(passed_votes)) <= 1

    # Merge criteria by position; a judge that returns fewer than the others only
    # counts where it said something.
    n_crit = max((len(v.criteria) for _n, v in votes), default=0)
    merged_criteria, split_criteria = [], []
    for i in range(n_crit):
        at_i = [v.criteria[i] for _n, v in votes if i < len(v.criteria)]
        if not at_i:
            continue
        yes = sum(c.passed for c in at_i)
        merged_criteria.append(CriterionResult(text=at_i[0].text, passed=yes * 2 > len(at_i)))
        if len(set(c.passed for c in at_i)) > 1:
            split_criteria.append(at_i[0].text)

    lead = next((v for _n, v in votes if v.passed == merged_passed), votes[0][1])
    tally = f"{sum(passed_votes)}/{len(passed_votes)} für bestanden"
    reason = lead.reason if unanimous else f"[{tally}, geteilt] {lead.reason}"
    merged = Verdict(criteria=merged_criteria, passed=merged_passed, reason=reason)
    agreement = {
        "unanimous": unanimous,
        "tally": tally,
        "votes": {name: v.passed for name, v in votes},
        "reasons": {name: v.reason for name, v in votes},
        "split_criteria": split_criteria,
    }
    return merged, agreement


async def judge_panel_run(members, test: dict, prompt: str, tools: list[str],
                          answer: str, scope: str = "", facts: str = ""):
    """Like :func:`judge_run`, over a panel — the same return plus ``agreement``.

    Returns ``(verdict, coverage, missing, effort, agreement)``. ``verdict`` is the
    majority verdict and an ordinary :class:`Verdict`, so archive, printout and UI
    carry on unchanged. Coverage and effort are deterministic from the tool chain and
    the same for every judge — they come from the first member.

    **One member failing does not bring the panel down.** It drops out of the vote and
    is listed in ``agreement["errors"]``; only when *no one* returns a verdict does the
    error surface — then there is nothing to take a majority of.
    """
    votes, errors = [], {}
    coverage = missing = effort = None
    for agent, name in members:
        try:
            v, cov, miss, eff = await judge_run(agent, test, prompt, tools, answer,
                                                scope=scope, facts=facts)
        except Exception as exc:  # noqa: BLE001 - a failure costs a vote, not the run
            errors[name] = f"{type(exc).__name__}: {exc}"
            continue
        votes.append((name, v))
        if coverage is None:
            coverage, missing, effort = cov, miss, eff
    if not votes:
        raise RuntimeError(f"kein Judge lieferte ein Urteil: {errors}")
    merged, agreement = _merge_verdicts(votes)
    agreement["errors"] = errors
    return merged, coverage, missing, effort, agreement


def tool_coverage(want: list[str], tools: list[str]):
    """Deterministic tool coverage: fraction of ``want`` satisfied by ``tools``.

    An entry may list interchangeable alternatives separated by ``|`` — e.g.
    ``"qgis_show_3d|render_buildings_3d"`` is satisfied by *either* call, since a
    test can have two equally correct routes (live QGIS vs. web 3D) and requiring
    both would make a single correct run unable to reach 100%.

    Returns ``(coverage, missing)``; ``coverage`` is ``None`` when nothing is
    expected. ``missing`` reports entries verbatim (alternatives included), so a
    report shows what was expected, not one arbitrary branch of it.
    """
    called = set(tools)
    missing = [
        entry for entry in want if not any(alt.strip() in called for alt in entry.split("|"))
    ]
    coverage = (len(want) - len(missing)) / len(want) if want else None
    return coverage, missing


def tool_effort(want: list[str], tools: list[str]) -> dict:
    """What the run *cost* in tool calls — recorded, deliberately not scored.

    ``tool_coverage`` only asks how much of the plan was reached, so a run that
    hits its three expected tools in thirty calls still scores 100%. This is the
    missing half: ``calls`` (every tool call), ``distinct`` (how many different
    tools), ``per_step`` (calls per planned tool — a detour factor) and
    ``offplan`` (distinct tools called that no ``tools_expected`` entry covers).

    **No pass/fail threshold, on purpose.** Measured over the 36 archived runs in
    ``history-pre-timing-20260728.jsonl``, call count barely separates the two
    outcomes (median 14 on PASS vs 13 on FAIL) — a budget check would fire on
    correct runs. Its worth is the trend and the runaway (max was 33), not the grade.

    ``offplan`` is likewise data, not a verdict: the same measurement shows it is
    dominated by tools Chester's own rules *require* — `geocode` (26 runs),
    `vector_info` (20), `check_crs` (8), `sanity_check_result` (7) — which the bank
    mostly does not list. A precision score over these names (median 0.42, and only
    0.47 vs 0.34 between PASS and FAIL) would report rule-following as imprecision.
    Read the list the other way round: a tool that keeps appearing here says the
    bank's ``tools_expected`` is incomplete, not that the agent went wandering.
    """
    expected = {alt.strip() for entry in want for alt in entry.split("|")}
    distinct = list(dict.fromkeys(tools))
    return {
        "calls": len(tools),
        "distinct": len(distinct),
        "per_step": round(len(tools) / len(want), 1) if want else None,
        "offplan": [t for t in distinct if t not in expected],
    }


async def judge_run(
    judge_agent, test: dict, prompt: str, tools: list[str], answer: str, scope: str = "",
    facts: str = "",
):
    """Grade one run: LLM verdict against the rubric + deterministic tool metrics.

    Returns ``(verdict, coverage, missing_tools, effort)``. ``coverage`` comes from
    ``tool_coverage`` over ``tools_expected`` (``None`` when the test lists none)
    — a cheap, exact check that needs no LLM, e.g. "did the agent call
    ``check_crs`` before reprojecting".

    Async (``await judge_agent.run(...)``, not ``run_sync``) so it works both from
    ``testprompt.py`` — wrapped in its own ``asyncio.run`` after the agent turn —
    and from inside ``evals.py``'s already-running batch loop.

    Refuses a transcript with **neither** a tool call nor an answer
    (:class:`TraceUnavailable`). Such a run cannot be told apart from one we simply
    failed to read back, and a verdict on nothing is worthless in both readings — but
    only one of them is honest. Better a loud gap in the history than a confident FAIL
    in it. A genuinely idle model still produces *some* text, so this costs no real
    verdict.
    """
    if not tools and not (answer or "").strip():
        raise TraceUnavailable(
            "the transcript has neither a tool call nor an answer — refusing to grade. "
            "Either the run produced nothing, or its trace was not read back; from here "
            "those look identical, and a FAIL would claim to know which."
        )
    lines = [
        f"# Task\n{prompt}",
        f"\n# Expected behaviour\n{test.get('expected_behavior') or '(none given)'}",
    ]
    criteria = test.get("success_criteria") or []
    if criteria:
        lines.append("\n# Success criteria\n" + "\n".join(f"- {c}" for c in criteria))
    lines.append(
        "\n# Agent tool-call sequence\n" + (" → ".join(tools) if tools else "(no tools called)")
    )
    if scope:
        lines.append(
            "\n# Tool calls with their arguments (verbatim from the trace — do not "
            "infer any of this from the sequence above)\n" + scope
        )
    if facts:
        lines.append(
            "\n# Layers this run produced, read from the files (CRS and size — do not "
            "infer these either; the last line is the final result)\n" + facts
        )
    lines.append("\n# Agent final answer\n" + (answer or "(empty)"))

    verdict = (await judge_agent.run("\n".join(lines))).output

    want = list(test.get("tools_expected") or [])
    coverage, missing = tool_coverage(want, tools)
    return verdict, coverage, missing, tool_effort(want, tools)


def print_verdict(  # noqa: PLR0913  # one print function per measure would be worse
    verdict: Verdict,
    coverage,
    missing,
    judge_name: str,
    self_grading: bool,
    effort: dict | None = None,
) -> None:
    """Print the ``--- judge ---`` block after a run."""
    print("\n--- judge ---\n")
    if self_grading:
        print("⚠ Judge model == model under test — verdict is self-referential.\n")
    print(f"Judge: {judge_name}\n")
    for c in verdict.criteria:
        print(f"  {'✓' if c.passed else '✗'} {c.text}")
    if coverage is not None:
        tail = f"  (missing: {', '.join(missing)})" if missing else ""
        print(f"\nTool coverage: {round(coverage * 100)}%{tail}")
    if effort:
        per = "" if effort["per_step"] is None else f", {effort['per_step']}× the plan"
        print(f"Tool calls: {effort['calls']} in {effort['distinct']} tool(s){per}")
        if effort["offplan"]:
            print(f"  off-plan: {', '.join(effort['offplan'])}")
    print(f"\nVerdict: {'PASS' if verdict.passed else 'FAIL'} — {verdict.reason}")
