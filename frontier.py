"""The counter-check: the same benchmark case, once with Chester, once with a bare
frontier model.

The compensation question (`doc/tool-compensation.md`) in its smallest testable form.
A **Test-Level-3** case from `agent-test-prompts.jsonl` runs twice:

* **Chester** — full model *plus* toolbox, graded as always by the judge against the
  case's `success_criteria`. This file changes nothing about that; it takes the
  verdict the run produces anyway.
* **Frontier bare** — a hosted model, **no** tool, **no** Chester instruction, no
  session. It gets the case's prompt and nothing else, and is graded with **the same
  rubric** by **the same** judge.

The yardstick is thus identical across both cells — that is the whole point of
sitting on Level 3: the bank already brings rubric and judge, while Level 2 judges
deliberately deterministically on the artifact and would have no measure at all for
the bare cell.

**What the comparison cannot do.** The bank runs `live`; without tools the frontier
model reaches no data. It therefore answers "does it know what would need doing?",
not "can it do it" — a gap measures the missing tools first. That is exactly why
`doc/tool-compensation.md` §2 actually plans *raw acquisition plus QGIS* for cell F−.
The bare cut here is the sharper, narrower question; whoever reads the numbers must
know the difference.

**Why the Claude API directly and not pydantic-ai** (decided 2026-09-02). Chester's
rule is "the LLM layer is config-only" — the counter-cell breaks it on purpose. The
gain: the bare cell is truly bare, without a framework that reshapes messages or
sets parameters, and provider-specific features (adaptive thinking, `effort`,
per-run token accounting) are available unobstructed. The price, and it belongs to
reading the numbers: the two cells now differ not only in the toolbox but also in
the client. The cell therefore stays as plain as possible — one call, no system
prompt, no tools, no sampling parameters.

The human has the last word: `record_comparison` records their own verdict,
separately from the judge, so the two can be read against each other afterwards.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_build import CONFIG_NAME, STATE_DIR

#: Where the comparisons are kept — one line per benchmark case and counter-check.
#: Beside `evals/history.jsonl`, not in it: there each line is *one* graded run, here
#: a pair plus the human verdict. Two shapes in one file would have turned every
#: analysis into a case distinction.
COMPARISON_PATH = Path(STATE_DIR) / "evals" / "frontier.jsonl"

#: Output cap of the bare cell. Generous because adaptive thinking counts towards it:
#: `max_tokens` limits thinking **and** answer together, so a tight value yields a
#: truncated answer with `stop_reason: max_tokens`.
_BARE_MAX_TOKENS = 32000

#: After this many characters without a line break the text line is written to the
#: log anyway. Without the cap, a model writing a very long paragraph without ``\n``
#: would sit in the buffer until the call ends — invisible exactly during the time
#: the live log is built for.
_LOG_LINE_FLUSH = 400


def frontier_model_name() -> str:
    """The ``evals.frontier_model`` string from the config (its own block, best effort)."""
    try:
        cfg = json.loads((Path(STATE_DIR) / CONFIG_NAME).read_text(encoding="utf-8"))
        return ((cfg.get("evals") or {}).get("frontier_model") or "").strip()
    except (OSError, ValueError):
        return ""


def bare_client(model_name: str, timeout_s: float):
    """An Anthropic client, nothing else.

    **Directly against the Claude API**, not via pydantic-ai like the rest of Chester
    (decision 2026-09-02). The price of that choice is in the module docstring; the
    gain is that the bare cell is truly bare — no framework that rewrites messages,
    attaches tools or sets parameters.

    Key resolution is left to the SDK: ``ANTHROPIC_API_KEY``, else
    ``ANTHROPIC_AUTH_TOKEN``, else a profile from ``ant auth login``. ``load_dotenv``
    pulls in the ``.env`` first — `test_app.py` was the only runner that did not call
    it, and so would never have seen the key.
    """
    from anthropic import AsyncAnthropic
    from dotenv import load_dotenv

    if not model_name_is_set(model_name):
        raise ValueError(
            f"kein Frontier-Modell gesetzt — `evals.frontier_model` in "
            f"{STATE_DIR}/{CONFIG_NAME} eintragen (z. B. \"claude-opus-4-8\") "
            f"und ANTHROPIC_API_KEY in .env hinterlegen."
        )
    load_dotenv()
    return AsyncAnthropic(timeout=timeout_s, max_retries=2)


def model_name_is_set(model_name: str) -> bool:
    """Is a model name set? (its own function so the UI asks the same question)"""
    return bool((model_name or "").strip())


def bare_model_id(model_name: str) -> str:
    """The model string for the Claude API — without provider prefix.

    The config may say ``anthropic/claude-opus-4-8`` (the form SelmaKit's
    ``build_model`` expects and the judge uses too). The Claude API wants the bare
    name. Accepting both removes the error source of a config entry having to look
    different depending on its consumer.
    """
    name = (model_name or "").strip()
    return name.split("/", 1)[1] if name.startswith("anthropic/") else name


def bare_log_path(test_id: str) -> Path:
    """Where this cell's live log goes — beside the Chester protocol of the same run.

    Same directory and the same UTC-stamped stem as ``testprompt.save_run_log``, with
    a ``.frontier.jsonl`` suffix, so the two cells of one comparison sort next to each
    other instead of having to be matched up by hand afterwards.
    """
    from testprompt import RUNS_DIR  # deferred, like `judge_panel_run` below

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return Path(RUNS_DIR) / f"{stamp}__{test_id}.frontier.jsonl"


class _LiveLog:
    """Append-only JSONL, flushed per record — readable *while* the call runs.

    Same reasoning as ``RunLogCapability`` (which does this for the Chester cell): a
    record that only appears at the end is missing exactly when it is needed — during
    a long call, and after one that died. Until 2026-09-09 the bare cell had no log at
    all and its answer lived only in Streamlit's ``session_state``, so closing the tab
    threw away a paid API call together with the token counts Phase KO needs for its
    cost estimate.

    Text is coalesced to whole lines before it is written: a record per streamed
    fragment would bury the structural entries under thousands of token-sized ones.
    Never raises — a log must not be able to fail the run it documents.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._pending = ""
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.path = None

    def write(self, kind: str, **fields: Any) -> None:
        if self.path is None:
            return
        try:
            record = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **fields}
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            pass

    def text(self, channel: str, chunk: str) -> None:
        """Buffer streamed text and emit whole lines as they complete."""
        self._pending += chunk
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self.write(channel, text=line)
        # A model can produce a very long single line; don't hold it hostage to a
        # newline that may never come.
        if len(self._pending) >= _LOG_LINE_FLUSH:
            self.write(channel, text=self._pending)
            self._pending = ""

    def close(self) -> None:
        if self._pending:
            self.write("text", text=self._pending)
            self._pending = ""


def _failed(log: _LiveLog, started: float, stop_reason: str, error: str) -> dict:
    """Close out a call that produced no answer — on disk as well as in the return.

    The failure paths are exactly the ones the log exists for: a timeout or a dropped
    connection is what a reader goes looking for afterwards. Whatever text had already
    streamed is flushed first, so a truncated answer stays readable.
    """
    log.close()
    log.write("failed", stop_reason=stop_reason, error=error,
              duration_s=round(time.monotonic() - started, 1))
    return {"answer": "", "duration_s": time.monotonic() - started,
            "stop_reason": stop_reason, "error": error, "usage": {}}


async def run_bare(model_name: str, prompt: str, timeout_s: float, *,
                   sink: Callable[[str], None] | None = None,
                   log_path: Path | None = None) -> dict:
    """Ask the prompt once. No tool, no system prompt, no session.

    **Streamed**, as the Claude API reference recommends for anything with long input
    or output: a non-streamed call with a large ``max_tokens`` runs into HTTP
    timeouts. ``get_final_message()`` then returns the complete answer.

    **Adaptive thinking is on**, and that is not an extra but parity: Chester's own
    model runs with ``"thinking": "high"`` from the config. Pitting a cell without
    thinking against one with it would introduce a second variable. ``temperature``
    and siblings are **not** set — on Opus 4.8 they are removed and answer with 400.

    Returns answer text, duration, stop reason and token usage; the usage is the basis
    for the cost estimate that Phase KO requires before the measurement runs.

    ``sink`` gets the stream as it arrives (the same one-callable contract
    ``ask.py`` uses for the Chester cell, so both sides of a comparison can be drawn
    into the UI the same way); ``log_path`` gets the same stream as JSONL on disk.
    Both are optional — without them this behaves exactly as it did before.

    ``display: "summarized"`` is set so that thinking, *when it happens*, is visible
    in the live view instead of arriving as empty blocks (the default is
    ``"omitted"``). Do not read more into it than that: measured 2026-09-09 against
    ``claude-sonnet-5`` at ``effort: high``, adaptive thinking produced **no thinking
    blocks at all** — on a one-sentence question and on a step-by-step CRS-ordering
    question alike, both settings returned a single ``text`` block and nothing else.
    So the setting is insurance for the prompts where the model does think, not a
    fix for an observed silent phase. It changes neither the billing nor ``answer``
    (that stays text blocks only, which is what the judge sees).
    """
    import anthropic

    client = bare_client(model_name, timeout_s)
    log = _LiveLog(log_path)
    started = time.monotonic()
    log.write("start", model=bare_model_id(model_name), prompt=prompt,
              max_tokens=_BARE_MAX_TOKENS, effort="high", thinking="adaptive",
              timeout_s=timeout_s)

    def emit(channel: str, chunk: str) -> None:
        if sink is not None:
            sink(chunk)
        log.text(channel, chunk)

    try:
        async with client.messages.stream(
            model=bare_model_id(model_name),
            max_tokens=_BARE_MAX_TOKENS,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "high"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for event in stream:
                if event.type != "content_block_delta":
                    continue
                delta = event.delta
                if delta.type == "text_delta":
                    emit("text", delta.text)
                elif delta.type == "thinking_delta":
                    emit("thinking", delta.thinking)
            message = await stream.get_final_message()
            log.close()
    except anthropic.APITimeoutError:
        return _failed(log, started, "timeout", f"Zeitdeckel {timeout_s:.0f}s")
    except anthropic.APIStatusError as exc:
        return _failed(log, started, "error", f"{type(exc).__name__}: {exc}")
    except anthropic.APIConnectionError as exc:
        return _failed(log, started, "error", f"Netzfehler: {exc}")

    # Read stop_reason **before** content: on a refusal content is empty or
    # truncated, and a blind content[0] would crash here.
    text = "".join(b.text for b in message.content if b.type == "text")
    usage = message.usage
    result = {
        "answer": text,
        "duration_s": time.monotonic() - started,
        "stop_reason": message.stop_reason,
        "error": "" if message.stop_reason in ("end_turn", "max_tokens") else str(
            getattr(message, "stop_details", "") or message.stop_reason),
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        },
    }
    log.write("final", stop_reason=result["stop_reason"], error=result["error"],
              duration_s=round(result["duration_s"], 1), usage=result["usage"],
              answer_chars=len(text))
    return result


async def judge_bare_run(judge_members, test: dict, prompt: str, model_name: str,
                         timeout_s: float, *,
                         sink: Callable[[str], None] | None = None,
                         log_path: Path | None = None) -> dict:
    """The bare cell: ask the prompt, grade the answer with the case's rubric.

    ``judge_members`` and ``test`` are the same ones the Chester run was just graded
    with — that is the condition for the two numbers to stand side by side at all.
    ``tools`` is empty and stays so: the cell has none, so coverage across both cells
    is not a shared figure either and is not kept here.
    """
    from testprompt import judge_panel_run

    run = await run_bare(model_name, prompt, timeout_s, sink=sink, log_path=log_path)
    cell = {
        "model": model_name,
        "duration_s": round(run["duration_s"], 1),
        "answer": run["answer"],
        "stop_reason": run["stop_reason"],
        "usage": run["usage"],
        # The path travels with the cell so the comparison record points at the log
        # instead of leaving a reader to guess the stem from a timestamp.
        "log_path": str(log_path) if log_path else "",
    }
    if not run["answer"].strip():
        # No verdict without an answer: `passed: None` means **ungraded**, not
        # failed. Counting a network error as FAIL would skew the comparison in
        # favour of the cell that ran.
        return {**cell, "passed": None,
                "reason": run["error"] or "keine Antwort", "criteria": []}
    if sink is not None:
        sink("\n\n[judge] benote die nackte Zelle …\n")
    judge_started = time.monotonic()
    verdict, _cov, _missing, _effort, agreement = await judge_panel_run(
        judge_members, test, prompt, [], run["answer"])
    # The panel is the long half of this cell (three local models loaded one after
    # another), so its outcome belongs in the log too — otherwise the file ends at
    # the API call and says nothing about the wait that followed it.
    _LiveLog(log_path).write("judged", passed=bool(verdict.passed), reason=verdict.reason,
                             panel=agreement,
                             judge_duration_s=round(time.monotonic() - judge_started, 1))
    return {**cell,
            "passed": bool(verdict.passed),
            "reason": verdict.reason,
            "criteria": [{"text": c.text, "passed": bool(c.passed)}
                         for c in verdict.criteria],
            "panel": agreement}


def comparison_record(test: dict, prompt: str, judge_name: str,
                      chester: dict, frontier: dict) -> dict:
    """Combine both cells into one archivable record.

    ``chester`` is the verdict the run produced anyway (the same shape as
    ``RunResult["verdict"]``), ``frontier`` the one from :func:`judge_bare_run`.
    """
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "test_id": test["id"],
        "category": test.get("category", ""),
        "prompt_de": prompt,
        "judge_model": judge_name,
        "chester": {
            "model": chester.get("model", ""),
            "passed": chester.get("passed"),
            "reason": chester.get("reason", ""),
            "coverage": chester.get("coverage"),
            "duration_s": chester.get("duration_s"),
            "answer": chester.get("answer", ""),
            "criteria": [{"text": txt, "passed": bool(ok)}
                         for txt, ok in (chester.get("criteria") or [])],
        },
        "frontier": frontier,
    }


def record_comparison(entry: dict, path: Path | None = None) -> None:
    """Append one comparison (judge verdicts **and** the human verdict)."""
    target = path or COMPARISON_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_comparisons(path: Path | None = None) -> list[dict]:
    """All comparisons, oldest first. Missing file → empty list."""
    target = path or COMPARISON_PATH
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows
