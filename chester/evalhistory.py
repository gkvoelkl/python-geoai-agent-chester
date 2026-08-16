"""Chester — eval history reader + aggregator (no LLM, no SelmaKit).

Reads the JSONL log that ``testprompt.py --judge`` / ``evals.py`` append to
(``.chester/evals/history.jsonl``, one judged run per line) and turns it into a
readable report: pass-rate + mean tool-coverage per model, and the latest verdict
per test. Shared by the ``evals.py --report`` CLI and the ``/eval`` slash command
so the two can't drift — the same spirit as ``geocache.py`` backing both
``data.py`` and the ``geocache_*`` tools.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

DEFAULT_HISTORY = Path(".chester") / "evals" / "history.jsonl"


def load_history(path: Path | str = DEFAULT_HISTORY) -> list[dict]:
    """Read every judged-run record (best-effort; blank/bad lines skipped)."""
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _fmt_pct(value) -> str:
    """A fraction as a rounded percent, or ``-`` when unknown (no tools expected)."""
    return "-" if value is None else f"{round(value * 100)}%"


def _fmt_ts(ts: str) -> str:
    """``2026-07-10T18:02:34+00:00`` → ``2026-07-10 18:02`` (minute precision)."""
    return (ts or "")[:16].replace("T", " ")


def _fmt_dur(seconds) -> str:
    """Seconds as ``42s`` / ``7.5min``, or ``-`` for a run archived without timing."""
    if seconds is None:
        return "-"
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.1f}min"


def per_model(records: list[dict]) -> list[dict]:
    """Aggregate pass-rate, mean tool-coverage, mean run time and mean tool calls per model.

    ``avg_duration`` averages only the runs that carry a ``duration_s`` (timing
    was added later, older rows have none) — so a half-timed history reports a
    mean over what it actually measured instead of silently counting a missing
    duration as zero. ``timed`` says how many runs that mean rests on.
    ``avg_calls`` follows the same rule for ``tool_calls``, which arrived later
    still: it is the *cost* half of the picture next to coverage's *reach*, and a
    run archived before the field existed must not read as a run that made zero
    calls.
    """
    runs: dict[str, int] = defaultdict(int)
    passed: dict[str, int] = defaultdict(int)
    cov_sum: dict[str, float] = defaultdict(float)
    cov_n: dict[str, int] = defaultdict(int)
    dur_sum: dict[str, float] = defaultdict(float)
    dur_n: dict[str, int] = defaultdict(int)
    call_sum: dict[str, int] = defaultdict(int)
    call_n: dict[str, int] = defaultdict(int)
    for r in records:
        model = r.get("model", "?")
        runs[model] += 1
        if r.get("passed"):
            passed[model] += 1
        cov = r.get("tool_coverage")
        if cov is not None:
            cov_sum[model] += cov
            cov_n[model] += 1
        dur = r.get("duration_s")
        if dur is not None:
            dur_sum[model] += dur
            dur_n[model] += 1
        calls = r.get("tool_calls")
        if calls is not None:
            call_sum[model] += calls
            call_n[model] += 1
    out = []
    for model in sorted(runs):
        out.append(
            {
                "model": model,
                "runs": runs[model],
                "passed": passed[model],
                "pass_rate": passed[model] / runs[model] if runs[model] else 0.0,
                "avg_coverage": cov_sum[model] / cov_n[model] if cov_n[model] else None,
                "avg_duration": dur_sum[model] / dur_n[model] if dur_n[model] else None,
                "timed": dur_n[model],
                "avg_calls": call_sum[model] / call_n[model] if call_n[model] else None,
                "counted": call_n[model],
            }
        )
    return out


def latest_per_test(records: list[dict]) -> list[dict]:
    """The most recent judged run per test id.

    The log is append-only in chronological order, so a later line for the same
    ``test_id`` simply overwrites the earlier one — the current state of the bank.
    Collapses across models (the model column names which one produced it).
    """
    latest: dict[str, dict] = {}
    for r in records:
        latest[r.get("test_id", "?")] = r
    return [latest[k] for k in sorted(latest)]


def format_report(records: list[dict], filter: str | None = None) -> str:
    """Render the history as a Markdown report (shared by CLI and ``/eval``).

    ``filter`` is a case-insensitive substring matched against test id OR model,
    so ``/eval crs`` narrows to CRS tests and ``/eval qwen`` to one model.
    """
    if filter:
        needle = filter.lower()
        records = [
            r
            for r in records
            if needle in str(r.get("test_id", "")).lower()
            or needle in str(r.get("model", "")).lower()
        ]
    if not records:
        if filter:
            return f"No eval history matching `{filter}`."
        return "No eval history yet — run `uv run evals.py` or `uv run testprompt.py <id> --judge`."

    models = per_model(records)
    tests = latest_per_test(records)
    n_models = len({r.get("model") for r in records})

    lines = [
        f"**Eval history** — {len(records)} judged run(s) · "
        f"{len(tests)} test(s) · {n_models} model(s)",
        "",
        "_By model:_",
        "| model | runs | pass | pass-rate | avg tools | avg calls | avg time |",
        "|---|---|---|---|---|---|---|",
    ]
    for m in models:
        # "(n)" after the mean time: how many runs were actually timed, so a mean
        # over 2 of 28 runs can't be mistaken for the model's overall speed. Not
        # shown when all runs are timed, nor when none are (the mean is "-" then).
        timed = "" if m["timed"] in (0, m["runs"]) else f" ({m['timed']})"
        counted = "" if m["counted"] in (0, m["runs"]) else f" ({m['counted']})"
        calls = "-" if m["avg_calls"] is None else f"{m['avg_calls']:.0f}{counted}"
        lines.append(
            f"| `{m['model']}` | {m['runs']} | {m['passed']} | "
            f"{_fmt_pct(m['pass_rate'])} | {_fmt_pct(m['avg_coverage'])} | "
            f"{calls} | {_fmt_dur(m['avg_duration'])}{timed} |"
        )
    lines += [
        "",
        "_Latest per test:_",
        "| test | verdict | cov | calls | time | model | when |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in tests:
        verdict = "✓ PASS" if r.get("passed") else "✗ FAIL"
        run_calls = r.get("tool_calls")
        lines.append(
            f"| `{r.get('test_id', '?')}` | {verdict} | "
            f"{_fmt_pct(r.get('tool_coverage'))} | {'-' if run_calls is None else run_calls} | "
            f"{_fmt_dur(r.get('duration_s'))} | "
            f"`{r.get('model', '?')}` | {_fmt_ts(r.get('ts', ''))} |"
        )
    return "\n".join(lines)
