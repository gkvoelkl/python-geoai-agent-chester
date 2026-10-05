"""Tool-usage ledger — one line per tool call, kept across sessions.

Why a file of its own. Every other record of a call is temporary or partial: the
session trace is deleted before every bench run (`--fresh`), probe and dialogue;
the run log is one file per session key; the bench log exists only for bench runs;
the MCP server writes into its own workspace. The coverage sensor read the session
traces alone and on 2026-10-05 reported 38 tools as never called — nine of them had
run, `spectral_index` among them, whose first call had found two defects. A question
like "which tools does the agent actually use, and when last?" needs one place that
only ever grows.

What a line holds: when (UTC), which tool, how it ended (``ok`` · ``fail`` = the
tool returned ``ok: false`` · ``error`` = it raised · ``invalid`` = the arguments
were refused), how long it took, which session and which caller (``agent`` or
``mcp``). No arguments and no result: this answers *whether* and *when*, the run log
answers *what*.

Pure core (no SelmaKit): the runtime observer and the MCP server both write here.

    uv run python -m chester.toolusage      # per tool: calls, failures, last used
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_LOG_DIR = ".chester/logs"
LEDGER = "tool-usage.jsonl"
OUTCOMES = ("ok", "fail", "error", "invalid", "unknown")
#: Outcomes that say something went wrong; ``unknown`` (backfilled history) is not one.
NOT_OK = ("fail", "error", "invalid")


def outcome_of(result: Any) -> str:
    """``fail`` for a tool that answered ``ok: false``, otherwise ``ok``.

    `ok: false` is a result, not an exception — counted as plain success it would
    hide exactly the calls worth a second look.
    """
    return "fail" if isinstance(result, dict) and result.get("ok") is False else "ok"


def record(tool: str, outcome: str, *, seconds: float | None = None,
           session: Any = None, source: str = "agent",
           log_dir: str | Path = DEFAULT_LOG_DIR) -> None:
    """Append one call. Never raises — a ledger must not be able to fail a run.

    Under pytest the *default* ledger is left alone: a test that drives a real agent
    through a tool would otherwise count as usage. An explicit ``log_dir`` (a test's
    ``tmp_path``) is written as always.
    """
    if "PYTEST_CURRENT_TEST" in os.environ and Path(log_dir) == Path(DEFAULT_LOG_DIR):
        return
    try:
        line: dict[str, Any] = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "tool": tool,
                "outcome": outcome, "source": source}
        if seconds is not None:
            line["seconds"] = seconds
        if session is not None:
            line["session"] = str(session)
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        # One `open(..., "a")` per line: concurrent writers (gateway, a CLI run, the
        # MCP server) interleave whole lines instead of overwriting each other.
        with open(directory / LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:  # noqa: BLE001 - a ledger never costs a result
        pass


def read(log_dir: str | Path = DEFAULT_LOG_DIR) -> list[dict]:
    """All calls, oldest first. A missing file or a torn last line costs nothing."""
    path = Path(log_dir) / LEDGER
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("tool"):
            entries.append(entry)
    return sorted(entries, key=lambda e: str(e.get("ts", "")))


def usage(entries: list[dict]) -> dict[str, dict]:
    """Per tool: number of calls, of failed ones (`NOT_OK`), first and last timestamp."""
    out: dict[str, dict] = {}
    for e in entries:
        row = out.setdefault(e["tool"], {"calls": 0, "not_ok": 0, "first": e.get("ts"),
                                         "last": e.get("ts")})
        row["calls"] += 1
        row["not_ok"] += e.get("outcome") in NOT_OK
        row["last"] = e.get("ts") or row["last"]
    return out


def local_time(ts: str | None, tz: str = "Europe/Berlin") -> str:
    """A ledger timestamp as local wall-clock time — the ledger stays UTC."""
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts).astimezone(ZoneInfo(tz)).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return ts


def main(argv: list[str] | None = None) -> int:
    """Print the usage table, most recently used first."""
    argv = sys.argv[1:] if argv is None else argv
    log_dir = argv[0] if argv else DEFAULT_LOG_DIR
    entries = read(log_dir)
    if not entries:
        print(f"no tool calls recorded in {Path(log_dir) / LEDGER}")
        return 0
    rows = sorted(usage(entries).items(), key=lambda kv: str(kv[1]["last"]), reverse=True)
    width = max(len(name) for name, _ in rows)
    print(f"{'tool':<{width}}  {'calls':>6}  {'not ok':>6}  last used (local)")
    for name, row in rows:
        print(f"{name:<{width}}  {row['calls']:>6}  {row['not_ok']:>6}  {local_time(row['last'])}")
    since = local_time(entries[0].get("ts"))
    print(f"\n{len(entries)} calls of {len(rows)} tools since {since} "
          f"(as of {time.strftime('%d.%m.%Y %H:%M')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
