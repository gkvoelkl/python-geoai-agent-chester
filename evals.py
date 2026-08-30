"""Chester — batch benchmark runner + aggregate report.

Runs the WHOLE test bank (or a ``--filter`` subset) against the live agent, judges
each run with the same LLM judge and archives each verdict — the batch companion
to ``testprompt.py``'s single-run ``--judge``. It reuses testprompt's exact
functions (``load_tests`` / ``build_judge`` / ``read_trace`` / ``judge_run`` /
``archive_run``) and the gateway's wiring, so batch and single-run can't drift.

``--report`` skips running entirely and just aggregates the accumulated
``.chester/evals/history.jsonl`` (no agent, no judge, no network) via
``chester.evalhistory`` — the same formatter the ``/eval`` slash command uses.

Usage:
    uv run evals.py                    # run + judge the whole bank, print a summary
    uv run evals.py --filter crs       # only tests whose id contains "crs"
    uv run evals.py --fresh            # clear the GeoCache before EACH test
    uv run evals.py --judge-model anthropic/claude-opus-4-8   # override the judge
    uv run evals.py --verbose          # also stream each agent run (default: quiet)
    uv run evals.py --judge-last       # all runs first, then all judging (one model swap)
    uv run evals.py --gate             # exit 1 if any test FAILs (CI-style gate)
    uv run evals.py --report           # no run; aggregate history.jsonl
    uv run evals.py --report --filter qwen   # filter the report by test id or model
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from selmakit import Gateway

from agent_build import (
    CONFIG_NAME,
    STATE_DIR,
    geo_capabilities,
    register_validation_gate,
    selmakit_capabilities,
)
from ask import ask
from chester import evalhistory
from setup import setup
from testprompt import (
    archive_run,
    build_judge,
    clear_geocache,
    clear_session,
    judge_run,
    layer_facts,
    load_tests,
    read_trace,
    save_run_log,
    scoping_notes,
    timestamped_sink,
    validation_note,
)

HISTORY_PATH = Path(STATE_DIR) / "evals" / "history.jsonl"


def select_tests(filter: str | None) -> list[dict]:
    """The test bank, optionally narrowed to ids containing ``filter``."""
    tests = load_tests()
    if filter:
        needle = filter.lower()
        tests = [t for t in tests if needle in t["id"].lower()]
    return tests


def shard_tests(tests: list[dict], spec: str) -> list[dict]:
    """The ``i``-th of ``n`` contiguous, balanced slices of ``tests`` (``spec="i/n"``).

    Splits the bank into ``n`` packages so it can be run one at a time; each test
    still archives individually, so ``--report`` aggregates across shards. Balanced:
    with 34 tests over 8 shards, the first 2 get 5 and the rest get 4. Raises
    ``ValueError`` on a malformed spec or out-of-range index.
    """
    try:
        i_str, n_str = spec.split("/", 1)
        i, n = int(i_str), int(n_str)
    except ValueError:
        raise ValueError(f"--shard must be 'i/n' (e.g. '3/8'), got {spec!r}") from None
    if n < 1 or not (1 <= i <= n):
        raise ValueError(f"--shard i/n needs 1 ≤ i ≤ n and n ≥ 1, got {spec!r}")
    k, m = divmod(len(tests), n)
    start = (i - 1) * k + min(i - 1, m)
    end = start + k + (1 if (i - 1) < m else 0)
    return tests[start:end]


async def run_batch(
    agent, judge, tests: list[dict], *, fresh: bool, verbose: bool, judge_last: bool = False
) -> list[dict]:
    """Run + judge + archive every test, printing a one-line result per test.

    With ``judge_last`` every agent run happens first and the grading follows in one
    block. Measured 2026-08-22: the tested model holds 27 GB of this machine's 32 GB,
    so the judge cannot stay resident beside it — interleaving pays one model swap
    per test in each direction. The trade is that a batch aborted mid-way leaves the
    finished runs ungraded (their protocols are still on disk).
    """
    _judge_agent, _judge_name, model_under_test, _self_grading = judge
    lang = "de"
    results = []
    pending: list[dict] = []
    total = len(tests)
    for i, test in enumerate(tests, 1):
        # A bank entry without a prompt is a broken record, not an empty task:
        # passing `None` (or "") to the agent spends a full run producing nothing
        # and then archives a verdict about it. Name it and move on.
        prompt = str(test.get("prompt_de") or "").strip()
        if not prompt:
            print(f"[{i}/{total}] {test['id']:<34} SKIP  ohne prompt_de", flush=True)
            continue
        session_key = f"eval:{test['id']}"
        if fresh:
            # Best-effort wipe before each test so it re-fetches from scratch —
            # both the GeoCache and the session (else the agent resumes the prior
            # chat, skips the work, and read_trace grades stale tool calls).
            with contextlib.suppress(OSError):
                clear_geocache()
            clear_session(session_key)
        # Run the agent. Quiet by default — 18 full traces would flood the console;
        # --verbose streams each run (with the tool exchange) like testprompt.py.
        started = time.monotonic()
        # The protocol is kept for every run, quiet or verbose — a batch is exactly
        # where you cannot watch, so the file is the only record afterwards. Quiet
        # mode still collects it; it just does not echo to the console.
        log_parts: list[str] = []
        if verbose:
            print(f"\n===== [{i}/{total}] {test['id']} =====", flush=True)
            sink = timestamped_sink(log_parts.append, lambda s: print(s, end="", flush=True))
        else:
            sink = timestamped_sink(log_parts.append)
        final_answer = await ask(
            agent, prompt, session_key=session_key, show_tools=True, sink=sink
        )
        duration_s = time.monotonic() - started
        # Same reason as in testprompt.py: the gate's advisory tier lives only in the
        # returned answer, so without this the batch record and the judge miss it.
        gate_note = validation_note(final_answer)
        if gate_note:
            log_parts.append(f"\n[gate] {gate_note}\n")
        log_path = save_run_log(
            test["id"],
            model_under_test,
            session_key,
            "".join(log_parts),
            duration_s=duration_s,
        )
        pending.append(
            {"i": i, "test": test, "prompt": prompt, "session_key": session_key,
             "protocol": "".join(log_parts), "duration_s": duration_s, "log_path": log_path,
             "final_answer": final_answer}
        )
        if judge_last:
            print(f"[{i}/{total}] {test['id']:<34} RUN   {duration_s / 60:.0f}min", flush=True)
            continue
        results.extend(await _judge_and_archive(judge, pending.pop(), lang, total, verbose))

    for item in pending:  # --judge-last: every run is done, the judge loads once
        results.extend(await _judge_and_archive(judge, item, lang, total, verbose))
    return results


async def _judge_and_archive(judge, item, lang: str, total: int, verbose: bool) -> list[dict]:
    """Grade one finished run and archive it; `[]` when the trace cannot be read.

    A one-element list (not a dict) so the caller can extend unconditionally —
    an unreadable trace simply contributes nothing. Split out of ``run_batch``
    so the batch can also grade *after* all runs (``--judge-last``).
    """
    judge_agent, judge_name, model_under_test, _self_grading = judge
    i, test, prompt = item["i"], item["test"], item["prompt"]
    duration_s, log_path = item["duration_s"], item["log_path"]
    if verbose:
        # Mark the switch from the agent turn to judging (a separate LLM call
        # that can be slow over a long transcript), mirroring testprompt.py.
        print(f"\n[judge] judging with {judge_name}…", flush=True)
    judge_started = time.monotonic()
    try:
        # Inside the guard: an unreadable trace must cost this one test, not the
        # batch — and must never be archived as if the agent had done nothing.
        tools, answer = read_trace(item["session_key"], item["protocol"])
        answer = item.get("final_answer") or answer  # judge what the caller got
        verdict, coverage, _missing, effort = await judge_run(
            judge_agent, test, prompt, tools, answer,
            scope=scoping_notes(item["session_key"]),
            facts=layer_facts(item["session_key"]),
        )
    except Exception as exc:  # noqa: BLE001 - one bad judge call must not abort the batch
        print(
            f"[{i}/{total}] {test['id']:<34} JUDGE-ERR  {type(exc).__name__}: {exc}",
            flush=True,
        )
        return []
    archive_run(
        test,
        prompt,
        lang,
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
    mark = "PASS" if verdict.passed else "FAIL"
    cov = "-" if coverage is None else f"{round(coverage * 100)}%"
    # flush: a batch runs for hours, and redirected stdout (`evals.py > log`)
    # is block-buffered — without it the per-test lines only appear at exit.
    print(
        f"[{i}/{total}] {test['id']:<34} {mark}  cov={cov}  "
        f"calls={effort['calls']}  {duration_s / 60:.0f}min",
        flush=True,
    )
    return [{"test": test, "verdict": verdict, "coverage": coverage, "effort": effort}]


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-run and judge Chester's benchmark bank.")
    parser.add_argument("--filter", help="only tests whose id contains this substring")
    parser.add_argument(
        "--shard", metavar="i/n", help="run the i-th of n contiguous packages (e.g. 3/8)"
    )
    parser.add_argument("--fresh", action="store_true", help="clear the GeoCache before each test")
    parser.add_argument(
        "--judge-model", metavar="PROVIDER/MODEL", help="override evals.judge_model"
    )
    parser.add_argument("--verbose", action="store_true", help="also stream each agent run")
    parser.add_argument(
        "--judge-last",
        action="store_true",
        help="run every test first, judge afterwards (avoids a model swap per test)",
    )
    parser.add_argument(
        "--gate", action="store_true", help="exit 1 if any test FAILs (CI-style gate)"
    )
    parser.add_argument("--report", action="store_true", help="do not run; aggregate history.jsonl")
    args = parser.parse_args()

    if args.report:
        records = evalhistory.load_history(HISTORY_PATH)
        print(evalhistory.format_report(records, filter=args.filter))
        return

    load_dotenv()  # hosted-provider keys (ANTHROPIC_API_KEY, …) from a local .env
    setup(quiet=True)

    # Build the judge up front so a missing/invalid config fails before any run.
    try:
        judge = build_judge(args.judge_model)
    except ValueError as exc:
        print(f"[judge] {exc}", file=sys.stderr)
        sys.exit(1)
    _agent, judge_name, model_under_test, self_grading = judge
    if self_grading:
        print("⚠ Judge model == model under test — verdicts are self-referential.\n")

    tests = select_tests(args.filter)
    if args.shard:
        try:
            tests = shard_tests(tests, args.shard)
        except ValueError as exc:
            print(f"[shard] {exc}", file=sys.stderr)
            sys.exit(1)
    if not tests:
        msg = f"No tests match {args.filter!r}." if args.filter else "No test prompts found."
        print(msg, file=sys.stderr)
        sys.exit(1)

    agent = Gateway.from_config(
        STATE_DIR,
        CONFIG_NAME,
        capabilities=selmakit_capabilities,
        extra_capabilities=geo_capabilities(),
    ).agent
    # Same wiring as `gateway.py`/`ask.py`: without the gate the batch grades an
    # agent one harness level below the product (see `testprompt.main`).
    register_validation_gate(agent)
    shard_note = f" · shard {args.shard}" if args.shard else ""
    print(
        f"Running {len(tests)} test(s){shard_note} · model={model_under_test} · judge={judge_name}"
    )
    print("  " + ", ".join(t["id"] for t in tests) + "\n")
    results = asyncio.run(
        run_batch(
            agent, judge, tests,
            fresh=args.fresh, verbose=args.verbose, judge_last=args.judge_last,
        )
    )

    passed = sum(1 for r in results if r["verdict"].passed)
    fails = [r for r in results if not r["verdict"].passed]
    print(f"\n=== {passed}/{len(results)} PASS ===")
    if fails:
        print("Failed:")
        for r in fails:
            print(f"  ✗ {r['test']['id']} — {r['verdict'].reason}")
    print("\nArchived to .chester/evals/history.jsonl · aggregate with `uv run evals.py --report`")

    if args.gate and fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
