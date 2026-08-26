"""Chester — benchmark test-prompt runner.

Drives the curated GeoBenchX-derived test prompts in
``agent-test-prompts.jsonl`` against the live agent. Without an argument it
lists every test; with a test id it prints that test's rubric (expected
behaviour + success criteria) and then streams the agent↔LLM exchange — each
tool call with its arguments and each result (both truncated when large),
followed by the final answer — so you can eyeball a single scenario end to end.

It borrows the gateway's exact wiring (``Gateway.from_config(...).agent``, no
channels started) and reuses ``ask.ask`` for streaming — same code path as the
CLI chat, so a test run behaves like a real conversation turn.

Usage:
    uv run testprompt.py                 # list all test prompts
    uv run testprompt.py --random        # run a random test from the bank
    uv run testprompt.py buffer-schools-500m   # run one test
    uv run testprompt.py buffer-schools-500m --fresh   # clear the GeoCache first
    uv run testprompt.py buffer-schools-500m --show    # open the rendered map in the browser
    uv run testprompt.py buffer-schools-500m --system   # also print the system prompt sent
    uv run testprompt.py buffer-schools-500m --judge    # grade the run (LLM judge) + archive
    uv run testprompt.py buffer-schools-500m --judge \
      --judge-model anthropic/claude-…          # override judge model

With ``--judge`` the run is scored after it finishes: a strict LLM judge (the
``evals.judge_model`` from the config, or ``--judge-model``) grades the final
answer against the test's ``expected_behavior``/``success_criteria``, a cheap
deterministic check measures how much of ``tools_expected`` was actually called,
and one line per judged run is appended to ``.chester/evals/history.jsonl`` (both
the tested model and the judge model, so the log doubles as a regression series
and a model comparison). The judge model is verified up front, so a missing
config fails before the (expensive) agent run.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import re
import shutil
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydantic import AliasChoices, BaseModel, Field
from selmakit import Gateway

from agent_build import (
    CONFIG_NAME,
    STATE_DIR,
    WORKSPACE_DIR,
    geo_capabilities,
    register_validation_gate,
    selmakit_capabilities,
)
from ask import ask
from chester.geocache import GeoCache
from setup import setup

PROMPTS_PATH = Path(__file__).resolve().parent / "agent-test-prompts.jsonl"
SESSIONS_DIR = Path(STATE_DIR) / "sessions"
HISTORY_PATH = Path(STATE_DIR) / "evals" / "history.jsonl"
# One protocol per run, kept. The session file cannot serve as the record: SelmaKit
# writes it per *session key*, so the next run of the same test overwrites it, and
# `--fresh` deletes it outright — the log of a run would live exactly until the run
# after it. Comparing a model against itself over time needs the old ones.
RUNS_DIR = Path(STATE_DIR) / "evals" / "runs"

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


def clear_session(session_key: str) -> None:
    """Delete the persisted session so a ``--fresh`` run starts a clean chat.

    The session_key is stable per test (``testprompt:<id>``), so without this a
    re-run resumes the *prior* conversation: the agent "remembers" finishing and
    skips the work, while ``read_trace`` reads the accumulated history and grades
    stale tool calls — a false PASS. Removes both the message log and its
    sidecar; a missing file is fine.
    """
    removed = False
    for suffix in (".json", ".meta.json"):
        try:
            (SESSIONS_DIR / f"{session_key}{suffix}").unlink()
            removed = True
        except OSError:
            pass
    if removed:
        print(f"[fresh] cleared session: {session_key}\n")


# Session-key prefixes the three runners use, so "has this test ever run" spans all
# of them: `testprompt:<id>` (CLI), `eval:<id>` (batch), `testapp:<id>` (bench).
_SESSION_PREFIXES = ("testprompt:", "eval:", "testapp:")


def _epoch(stamp: str, fmt: str) -> float:
    """A timestamp string as epoch seconds; unparsable → ``0.0`` (= never)."""
    try:
        return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def last_used(test_ids: list[str]) -> dict[str, float]:
    """When each test last ran, as epoch seconds — ``0.0`` for never.

    Three records outlive a run and **none of them alone is complete**, so the
    latest of the three wins:

    - the kept **protocols** (`.chester/evals/runs/<UTC>__<id>.log`) — every runner
      writes one, judged or not, but only since 0.1.3;
    - the **eval history** — reaches further back, yet holds *judged* runs only;
    - the **session files** (`<prefix><id>.json`) — the deepest record, but
      `--fresh` deletes one at the start of a run and a run that dies mid-stream
      never writes it back.

    Taking the maximum is the point: reading any single source alone would call a
    test "never used" that ran twenty times, and then keep proposing it.
    """
    seen = dict.fromkeys(test_ids, 0.0)

    def note(test_id: str, when: float) -> None:
        if test_id in seen and when > seen[test_id]:
            seen[test_id] = when

    with contextlib.suppress(OSError):
        for log in RUNS_DIR.glob("*.log"):
            stamp, _, test_id = log.stem.partition("__")
            note(test_id, _epoch(stamp, "%Y%m%dT%H%M%SZ"))
    with contextlib.suppress(OSError, ValueError):
        for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            with contextlib.suppress(ValueError, TypeError, KeyError):
                row = json.loads(line)
                note(row["test_id"], datetime.fromisoformat(row["ts"]).timestamp())
    with contextlib.suppress(OSError):
        for session in SESSIONS_DIR.glob("*.json"):
            if session.name.endswith(".meta.json"):
                continue
            for prefix in _SESSION_PREFIXES:
                if session.stem.startswith(prefix):
                    note(session.stem[len(prefix) :], session.stat().st_mtime)
                    break
    return seen


def pick_stalest_test(tests: list[dict]) -> str | None:
    """The test most overdue for a run: never used, else idle longest.

    One rule covers both halves of that sentence — never-used sorts as ``0.0``, so
    it simply *is* the oldest. The bank's own order breaks the tie, which matters
    when nothing has run yet: pressing this repeatedly then sweeps the bank in a
    stable order instead of jumping around.
    """
    if not tests:
        return None
    stamps = last_used([t["id"] for t in tests])
    order = {t["id"]: i for i, t in enumerate(tests)}
    return min(order, key=lambda test_id: (stamps[test_id], order[test_id]))


def clear_geocache() -> None:
    """Wipe the GeoCache working dir so a test starts from scratch.

    Removes every cached dataset, its provenance sidecar and the inventory
    itself; the directory is recreated on the next write. The path comes from
    ``GeoCache`` (not hardcoded here) so it can't drift from the tools.
    """
    geocache_dir = GeoCache(workspace=WORKSPACE_DIR).geocache_dir
    if geocache_dir.exists():
        shutil.rmtree(geocache_dir)
        print(f"[fresh] cleared geocache: {geocache_dir}\n")
    else:
        print(f"[fresh] geocache already empty: {geocache_dir}\n")


def _existing_html(path: str) -> Path | None:
    """Resolve a tool-returned HTML path to an existing file (else ``None``)."""
    if not isinstance(path, str) or not path.lower().endswith(".html"):
        return None
    candidate = Path(path)
    if candidate.exists():
        return candidate
    # A bare filename (or a path spelled from another cwd) lives in the GeoCache.
    cached = GeoCache(workspace=WORKSPACE_DIR).geocache_dir / candidate.name
    return cached if cached.exists() else None


def run_html(session_key: str) -> str | None:
    """Path to the HTML page this run produced — map *or* 3D view, newest wins.

    ``render_map`` drops a ``geocache/last_map.json`` pointer (html + layers, the
    one ``/qgis`` reuses), but the other renderers (``render_buildings_3d``,
    standalone WMS maps …) do not — so the pointer alone misses a 3D-only run.
    We therefore read the run's *own* trace and take the last ``.html`` path a
    tool returned, falling back to the pointer. Nothing rendered → ``None``.
    """
    try:
        messages = json.loads((SESSIONS_DIR / f"{session_key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        messages = []
    found: Path | None = None
    for msg in messages:
        for part in msg.get("parts", []):
            if part.get("part_kind") != "tool-return":
                continue
            content = part.get("content")
            values = content.values() if isinstance(content, dict) else [content]
            for value in values:
                hit = _existing_html(value)
                if hit is not None:
                    found = hit
    if found is not None:
        return str(found)

    pointer = GeoCache(workspace=WORKSPACE_DIR).geocache_dir / "last_map.json"
    try:
        html = json.loads(pointer.read_text(encoding="utf-8")).get("html")
    except (OSError, ValueError):
        return None
    hit = _existing_html(html) if html else None
    return str(hit) if hit else None


def open_last_map(session_key: str) -> None:
    """Open the HTML page this run rendered in the browser (if there is one)."""
    html = run_html(session_key)
    if not html:
        print("\n[show] no HTML output this run (no map / 3D view was rendered).")
        return
    print(f"\n[show] opening in browser: {html}")
    webbrowser.open(Path(html).resolve().as_uri())


def load_tests() -> list[dict]:
    """Read the JSONL test bank (one test object per line)."""
    tests = []
    for line in PROMPTS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            tests.append(json.loads(line))
    return tests


def print_list(tests: list[dict]) -> None:
    """Print a readable three-line block per test (id, prompt, metadata)."""
    if not tests:
        print("No test prompts found.")
        return
    for t in tests:
        prompt = t.get("prompt_de", "")
        meta = t.get("category", "-")
        print(t["id"])
        print(f"  {prompt}")
        print(f"  {meta}\n")
    print(f"{len(tests)} test(s) · run one with:  uv run testprompt.py <id>")


def timestamped_sink(*writers):
    """Wrap ``ask``'s chunk stream so every line carries a time and a gap.

    One implementation for both front ends — the terminal runner and the web bench
    print the same protocol, which is the whole point of them sharing ``ask``. Pass
    several writers to tee (log buffer *and* stdout).

    The gap sits on the line that **ends** the wait, so a slow tool or a long model
    turn reads as a number instead of as a pause you had to sit through. Stamping
    happens per line, not per chunk, because tokens arrive in fragments.
    """
    state = {"last": time.monotonic(), "at_line_start": True}

    def sink(chunk: str) -> None:
        out = []
        for piece in chunk.splitlines(keepends=True):
            if state["at_line_start"] and piece.strip():
                now = time.monotonic()
                gap = now - state["last"]
                state["last"] = now
                # "+0.0s" on a burst of tokens is noise; only a real wait gets a number.
                span = f"+{gap:5.1f}s" if gap >= 0.1 else " " * 7
                out.append(f"{datetime.now().strftime('%H:%M:%S')} {span} │ ")
                state["at_line_start"] = False
            out.append(piece)
            if piece.endswith("\n"):
                state["at_line_start"] = True
        text = "".join(out)
        for write in writers:
            write(text)

    return sink


def save_run_log(
    test_id: str, model: str, session_key: str, log_text: str, *, duration_s: float | None = None
) -> Path:
    """Keep this run's protocol, and a copy of its trace, under ``evals/runs/``.

    Two files per run, named by UTC start time so they sort chronologically and no
    two runs collide: ``<ts>__<test>.log`` (the timestamped protocol) and
    ``<ts>__<test>.trace.json`` (the session file as it stood *after this run*).
    The copy is what makes the transcript view work for a past run at all — the live
    session file belongs to whichever run went last.

    Best-effort: a failure to archive must never cost the run it documents.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{stamp}__{test_id}"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{stem}.log"
    header = [
        f"# {test_id}",
        f"model:       {model}",
        f"session_key: {session_key}",
        f"started_utc: {stamp}",
        f"duration_s:  {duration_s:.1f}" if duration_s is not None else "duration_s:  -",
        "",
    ]
    path.write_text("\n".join(header) + (log_text or "(kein Protokoll)"), encoding="utf-8")
    with contextlib.suppress(OSError):
        trace = SESSIONS_DIR / f"{session_key}.json"
        if trace.exists():
            shutil.copy2(trace, RUNS_DIR / f"{stem}.trace.json")
    return path


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


def read_trace(session_key: str, protocol: str = "") -> tuple[list[str], str]:
    """Extract the run's tool sequence and final answer from the persisted trace.

    SelmaKit persists every session as a list of messages at
    ``.chester/sessions/<key>.json`` (the record ``trace.py`` renders). We read
    the tool-call names (in order) and concatenate the ``text`` parts as the
    agent's answer — no extra plumbing in the agent path, since the run already
    ran under ``session_key``.

    Raises :class:`TraceUnavailable` when that file is missing or unreadable *and*
    ``protocol`` — the streamed run protocol, which the callers hold anyway —
    yields nothing either. The callers already treat a judging failure as "report
    it, archive nothing, the agent run itself is unaffected", which is exactly
    right here.
    """
    path = SESSIONS_DIR / f"{session_key}.json"
    try:
        messages = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        tools, outcome = trace_from_protocol(protocol)
        if tools or outcome:
            return tools, outcome
        raise TraceUnavailable(
            f"no readable session trace at {path} — the run may well have worked; "
            f"what failed is reading it back. Check that the run used session key "
            f"'{session_key}' and that the process persisted it."
        ) from exc
    tools, texts = [], []
    for msg in messages:
        for part in msg.get("parts", []):
            kind = part.get("part_kind")
            if kind == "tool-call":
                tools.append(part.get("tool_name", "?"))
            elif kind == "text" and part.get("content"):
                texts.append(part["content"])
    return tools, "\n".join(texts).strip()


def config_model_name() -> str:
    """The model under test (``model.model``), for the run log's header.

    Read straight from the config rather than taken from ``build_judge``: the log is
    written for every run, judged or not, and a protocol that does not say which
    model produced it is not comparable to the next one.
    """
    try:
        cfg = json.loads((Path(STATE_DIR) / CONFIG_NAME).read_text(encoding="utf-8"))
        return ((cfg.get("model") or {}).get("model") or "?").strip()
    except (OSError, ValueError):
        return "?"


def _load_judge_model_string() -> str:
    """The ``evals.judge_model`` string from the config (a custom block, best-effort)."""
    try:
        cfg = json.loads((Path(STATE_DIR) / CONFIG_NAME).read_text(encoding="utf-8"))
        return ((cfg.get("evals") or {}).get("judge_model") or "").strip()
    except (OSError, ValueError):
        return ""


def build_judge(override: str | None):
    """Build the judge agent, or raise ``ValueError`` if no judge model is set.

    The judge model comes from ``--judge-model`` (``override``) or the config's
    ``evals.judge_model``. It must be *independent* of the model under test, so a
    match is surfaced (self-grading) rather than silently accepted. The judge
    reuses SelmaKit's ``build_model`` by copying the main ``ModelConfig`` and
    overriding only ``.model`` — this keeps the Ollama base URL/timeout for a
    local judge, while a hosted judge (e.g. ``anthropic/…``) reads its own key.

    Returns ``(judge_agent, judge_name, model_under_test, self_grading)``.
    """
    from pydantic_ai import Agent
    from selmakit.config import build_model, load_config

    cfg = load_config(STATE_DIR, CONFIG_NAME)
    model_under_test = cfg.model.model
    judge_name = (override or "").strip() or _load_judge_model_string()
    if not judge_name:
        raise ValueError(
            f"no judge model configured — set `evals.judge_model` in "
            f"{STATE_DIR}/{CONFIG_NAME} or pass --judge-model <provider/model>."
        )
    judge_cfg = cfg.model.model_copy(update={"model": judge_name})
    # retries=3 (not the default 1): on a malformed Verdict pydantic-ai feeds the
    # validation error back to the judge, which usually self-corrects. Weak local
    # judges (e.g. a 12B) often get the nested schema wrong on the first try.
    agent = Agent(
        build_model(judge_cfg),
        output_type=Verdict,
        system_prompt=JUDGE_SYSTEM,
        retries=3,
    )
    return agent, judge_name, model_under_test, judge_name == model_under_test


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


_SCOPING_ARGS = ("place", "bbox")


def scoping_notes(session_key: str, limit: int = 12) -> str:
    """How each call scoped its area — the one argument the judge must not guess.

    The judge sees tool *names* only. Asked whether a run clipped to the city or
    worked off a rectangle, it therefore infers from the sequence — and on
    2026-08-23 it inferred wrong: `restaurant-heatmap` fetched with
    ``place="Regensburg, Bayern, Deutschland"``, and the verdict read "the double
    use of geocode followed by osm_features strongly suggests a bounding-box-based
    extraction". A criterion about arguments cannot be graded from names, so the
    arguments come along — these two keys, nothing else, to keep the judge prompt
    small and the temptation to re-derive the whole run out of it.

    Returns "" when no call carried either key (then the section is omitted).
    """
    try:
        messages = json.loads((SESSIONS_DIR / f"{session_key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    notes: list[str] = []
    for msg in messages:
        for part in msg.get("parts", []):
            if part.get("part_kind") != "tool-call":
                continue
            args = part.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    continue
            if not isinstance(args, dict):
                continue
            shown = {k: args[k] for k in _SCOPING_ARGS if k in args}
            if shown:
                pairs = ", ".join(
                    f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in shown.items()
                )
                notes.append(f"{part.get('tool_name', '?')}({pairs})")
    return "\n".join(notes[:limit])


def _produced_paths(messages: list) -> list[str]:
    """Every file path a tool return claims to have written, in call order.

    Two shapes cover the tool surface: a connector returns ``output``, a QGIS
    algorithm returns ``results.OUTPUT``. Duplicates are dropped so a layer
    rewritten twice is listed once, at its first appearance.
    """
    paths: list[str] = []
    for msg in messages:
        for part in msg.get("parts", []):
            if part.get("part_kind") != "tool-return":
                continue
            content = part.get("content")
            if not isinstance(content, dict):
                continue
            produced = content.get("output")
            if not isinstance(produced, str):
                results = content.get("results")
                produced = (results or {}).get("OUTPUT") if isinstance(results, dict) else None
            if isinstance(produced, str) and produced not in paths:
                paths.append(produced)
    return paths


def layer_facts(session_key: str, limit: int = 10) -> str:
    """CRS and size of the layers this run produced — read from the files themselves.

    The judge sees tool names and, since `scoping_notes`, the extent arguments. It
    still cannot see a **coordinate system**, and several tests grade one ("in einem
    metrischen CRS berechnet, nicht in Grad", "Haltestellen in EPSG:4326"). Measured
    2026-08-23 (`gtfs-stops-departures-map-regensburg`): the delivered layer was
    EPSG:25832 and the judge ticked the EPSG:4326 criterion anyway — a false PASS,
    the mirror image of the false FAIL that `scoping_notes` fixed. Both come from the
    same habit: asked for a fact it cannot see, the judge guesses.

    Read with ``geofacts.vector_facts`` (a header read, no geometry), newest layer
    last, so the final result is the last line. A layer that has since been pruned
    from the cache contributes nothing rather than a guess.
    """
    from chester import geofacts

    try:
        messages = json.loads((SESSIONS_DIR / f"{session_key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    lines: list[str] = []
    paths = _produced_paths(messages)
    for produced in paths[-limit:]:
        if not Path(produced).is_file() or Path(produced).suffix.lower() in {".html", ".png"}:
            continue
        try:
            f = geofacts.vector_facts(produced)
        except Exception:  # noqa: BLE001 - a diagnostic must never break the judging
            continue
        lines.append(
            f"{Path(produced).name}: {f.get('crs') or 'no CRS'}, "
            f"{f.get('feature_count')} features"
        )
    return "\n".join(lines)


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
            "\n# How the area was scoped (verbatim from the trace — do not infer this "
            "from the sequence above)\n" + scope
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


def print_verdict(  # noqa: PLR0913  # eine Druckfunktion je Kennzahl waere schlechter
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


def archive_run(  # noqa: PLR0913  # eine Zeile der Eval-Historie; jedes Feld ist eine
    # eigene Spalte im Protokoll, ein Sammelobjekt verschoebe die Struktur nur
    test,
    prompt,
    lang,
    model_under_test,
    judge_name,
    tools,
    coverage,
    verdict,
    *,
    duration_s: float | None = None,
    judge_duration_s: float | None = None,
    effort: dict | None = None,
    log: str | None = None,
) -> Path:
    """Append one JSONL line per judged run to ``.chester/evals/history.jsonl``.

    Carries both the tested model and the judge model, so the log is at once a
    regression series (same model over time) and a model comparison.

    ``duration_s`` is the wall-clock time of the *agent* turn (the interesting
    number: how long this model took on this task), ``judge_duration_s`` that of
    the grading call — kept apart so a slow judge never distorts the model
    comparison. ``effort`` is :func:`tool_effort`'s dict, ``log`` the path to this
    run's kept protocol — the verdict says *what* was decided, the log is the only
    way to see *why*, and without the link the two are related by nothing but a
    timestamp. All optional: rows written before a field existed simply lack it, and
    every reader treats a missing one as unknown rather than as zero.
    """
    effort = effort or {}
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "test_id": test["id"],
        "category": test.get("category"),
        "lang": lang,
        "model": model_under_test,
        "judge_model": judge_name,
        "duration_s": round(duration_s, 1) if duration_s is not None else None,
        "judge_duration_s": round(judge_duration_s, 1) if judge_duration_s is not None else None,
        "tool_coverage": coverage,
        "tool_calls": effort.get("calls"),
        "tools_distinct": effort.get("distinct"),
        "calls_per_step": effort.get("per_step"),
        "tools_offplan": effort.get("offplan"),
        "tools_called": tools,
        "tools_expected": test.get("tools_expected") or [],
        "log": log,
        "criteria": [{"text": c.text, "passed": c.passed} for c in verdict.criteria],
        "passed": verdict.passed,
        "reason": verdict.reason,
    }
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return HISTORY_PATH


def print_rubric(test: dict, prompt: str) -> None:
    """Print the test's expectation before the agent runs, for eyeballing."""
    print(f"=== {test['id']} · {test.get('category', '-')} ===")
    print(f"\nPrompt:\n  {prompt}")
    if test.get("expected_behavior"):
        print(f"\nExpected behaviour:\n  {test['expected_behavior']}")
    if test.get("success_criteria"):
        print("\nSuccess criteria:")
        for c in test["success_criteria"]:
            print(f"  - {c}")
    print("\n--- agent ---\n")


def main() -> None:  # noqa: C901, PLR0915
    # Ausnahme: argparse-Aufbau plus Ablaufsteuerung; das Aufteilen ergaebe
    # Hilfsfunktionen mit genau einem Aufrufer.

    parser = argparse.ArgumentParser(description="List or run Chester's benchmark test prompts.")
    parser.add_argument("test_id", nargs="?", help="id of the test to run (omit to list all)")
    parser.add_argument(
        "--random", action="store_true", help="pick and run a random test from the bank"
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="clear the GeoCache before the run (start from scratch)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="open the rendered map / 3D view in the browser after the run",
    )
    parser.add_argument(
        "--system", action="store_true", help="print the system prompt actually sent, after the run"
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="grade the run with an LLM judge and archive the verdict",
    )
    parser.add_argument(
        "--judge-model",
        metavar="PROVIDER/MODEL",
        help="judge model (overrides evals.judge_model in the config)",
    )
    args = parser.parse_args()

    tests = load_tests()

    # Declared before the branch: one arm looks the id up and may find nothing.
    test: dict | None
    if args.random:
        if not tests:
            print("No test prompts found.", file=sys.stderr)
            sys.exit(1)
        test = random.choice(tests)
        print(f"[random] selected test: {test['id']}\n")
    elif not args.test_id:
        print_list(tests)
        return
    else:
        test = next((t for t in tests if t["id"] == args.test_id), None)
        if test is None:
            ids = ", ".join(t["id"] for t in tests)
            print(f"Unknown test id: {args.test_id!r}\nAvailable: {ids}", file=sys.stderr)
            sys.exit(1)

    prompt = test["prompt_de"]

    load_dotenv()  # hosted-provider keys (ANTHROPIC_API_KEY, …) from a local .env

    # Build the judge up front — before printing the rubric or the expensive
    # agent run — so a missing/invalid judge config is the only output.
    judge = None
    if args.judge:
        try:
            judge = build_judge(args.judge_model)
        except ValueError as exc:
            print(f"[judge] {exc}", file=sys.stderr)
            sys.exit(1)

    print_rubric(test, prompt)
    setup(quiet=True)

    session_key = f"testprompt:{test['id']}"
    if args.fresh:
        # Clear before building the agent so its startup GeoCache scan sees an
        # empty cache — the run then re-fetches everything from scratch. Wipe the
        # session too, else the agent resumes the prior chat, "remembers" the
        # result and skips the work (a false PASS on stale tool calls).
        clear_geocache()
        clear_session(session_key)
    agent = Gateway.from_config(
        STATE_DIR,
        CONFIG_NAME,
        capabilities=selmakit_capabilities,
        extra_capabilities=geo_capabilities(),
    ).agent
    # The enforcing validation gate is part of the wiring under test, not an extra:
    # `gateway.py` and `ask.py` register it, so a benchmark without it grades an
    # agent the product never runs (measured 2026-08-22: zero output validators —
    # every history entry before that date was scored one level below H3).
    register_validation_gate(agent)
    # Show the agent↔LLM tool exchange (calls + args + results, truncated), so a
    # benchmark run reads as the full trace, not just the final answer.
    started = time.monotonic()
    # Tee: the protocol is kept *and* still printed. Timestamps come from the shared
    # sink, so terminal and web bench produce the same record.
    log_parts: list[str] = []
    asyncio.run(
        ask(
            agent,
            prompt,
            session_key=session_key,
            show_tools=True,
            sink=timestamped_sink(log_parts.append, lambda s: print(s, end="", flush=True)),
        )
    )
    duration_s = time.monotonic() - started
    print(f"\n[run] {duration_s:.0f}s")
    log_path = save_run_log(
        test["id"],
        config_model_name(),
        session_key,
        "".join(log_parts),
        duration_s=duration_s,
    )
    print(f"[run] Protokoll: {log_path}")

    if judge is not None:
        judge_agent, judge_name, model_under_test, self_grading = judge
        # A separate LLM call grades the run — print a marker so it's clear the
        # process moved from the agent turn to judging (and isn't hung), since a
        # local judge can take a while over a long transcript.
        print(f"\n[judge] judging with {judge_name}…", flush=True)
        judge_started = time.monotonic()
        try:
            # Reading the trace sits *inside* the guard: it can fail too, and it fails
            # after the expensive part is already done.
            tools, answer = read_trace(session_key, "".join(log_parts))
            verdict, coverage, missing, effort = asyncio.run(
                judge_run(judge_agent, test, prompt, tools, answer,
                          scope=scoping_notes(session_key),
                          facts=layer_facts(session_key))
            )
        except Exception as exc:  # noqa: BLE001 - a judge failure must not crash the run
            # The agent run already happened; a grading failure (e.g. a weak judge
            # that can't produce the Verdict schema) shouldn't lose it or archive a
            # bogus FAIL. Report and move on.
            print(f"\n[judge] could not grade this run: {type(exc).__name__}: {exc}")
            print(
                "[judge] not archived — the agent run itself is unaffected. "
                "Try a more reliable judge: --judge-model <provider/model>."
            )
        else:
            print_verdict(verdict, coverage, missing, judge_name, self_grading, effort)
            path = archive_run(
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
            print(f"\n[judge] archived to {path}")

    if args.show:
        open_last_map(session_key)

    if args.system:
        # Rendered instructions are stripped from the persisted history and cached
        # in the session metadata; None before the first LLM turn, so read it here.
        system_prompt = agent.last_system_prompt(session_key=session_key)
        print("\n--- system prompt ---\n")
        print(system_prompt if system_prompt else "(no system prompt recorded)")


if __name__ == "__main__":
    main()
