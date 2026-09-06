"""Chester — session trace viewer.

Pretty-prints what the agent actually did in a session: the prompt, the model's
reasoning (`thinking`), every tool call with its arguments, every tool result,
and the final answer. This is the full record SelmaKit already persists to
``.chester/sessions/<key>.json`` — no OpenTelemetry needed.

Usage:
    uv run trace.py                 # list sessions (newest first)
    uv run trace.py <key>           # print the trace for a session (e.g. cli)
    uv run trace.py last            # print the trace for the most recent session
    uv run trace.py <key> --full    # do not truncate long content
    uv run trace.py <key> --system  # also show the system prompt
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from selmakit import load_session_messages, load_session_meta

from chester.capabilities.runlog import DEFAULT_LOG_DIR as RUNLOG_DIR

SESSIONS_DIR = Path(".chester/sessions")

KIND_STYLE = {
    "user-prompt": ("👤", "You"),
    "thinking": ("🧠", "thinking"),
    "tool-call": ("🔧", "call"),
    "tool-return": ("✅", "result"),
    "text": ("💬", "Chester"),
    "system-prompt": ("⚙️", "system"),
}


def _clip(value, full: bool, limit: int = 600) -> str:
    text = str(value).strip()
    if full or len(text) <= limit:
        return text
    return text[:limit] + f"\n      … (+{len(text) - limit} chars, use --full)"


def session_files() -> list[Path]:
    """Session trace files, newest first (excluding SelmaKit's `.meta.json`)."""
    files = [
        f for f in SESSIONS_DIR.glob("*.json") if not f.name.endswith(".meta.json")
    ]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def last_system_prompt(key: str) -> str | None:
    """The system prompt as last sent for this session.

    SelmaKit strips the rendered instructions from the persisted message history
    and caches them in the session metadata (`<key>.meta.json`), so read them
    from there rather than from now-absent `system-prompt` message parts. Mirrors
    `Agent.last_system_prompt`, but without building an agent.
    """
    meta_path = SESSIONS_DIR / f"{key}.meta.json"
    if not meta_path.exists():
        return None
    try:
        return load_session_meta(SESSIONS_DIR, meta_path.stem.removesuffix(".meta")).get(
            "last_system_prompt"
        )
    except Exception:
        return None


def list_sessions() -> None:
    files = session_files()
    if not files:
        print(f"No sessions in {SESSIONS_DIR}/")
        return
    print(f"Sessions in {SESSIONS_DIR}/ (newest first):\n")
    for f in files:
        ts = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        kb = f.stat().st_size / 1024
        print(f"  {f.stem:<16} {ts}   {kb:6.1f} KB")
    print("\nView one with:  uv run trace.py <key>")


def show(key: str, full: bool, show_system: bool) -> None:
    path = SESSIONS_DIR / f"{key}.json"
    if not path.exists():
        print(f"No such session: {path}")
        print("Run `uv run trace.py` to list available sessions.")
        sys.exit(1)

    messages = load_session_messages(SESSIONS_DIR, path.stem)
    print(f"━━━ trace: {key} ━━━  ({len(messages)} message(s))\n")

    if show_system:
        icon, label = KIND_STYLE["system-prompt"]
        prompt = last_system_prompt(key)
        print(f"{icon} {label}: {_clip(prompt or '(none recorded)', full)}\n")

    for msg in messages:
        for part in msg.get("parts", []):
            kind = part.get("part_kind", "?")
            if kind == "system-prompt":
                continue  # stripped from history; shown from metadata above
            icon, label = KIND_STYLE.get(kind, ("•", kind))

            if kind == "tool-call":
                args = part.get("args")
                print(f"{icon} {label}: {part.get('tool_name')}({_clip(args, full, 400)})")
            elif kind == "tool-return":
                outcome = part.get("outcome", "?")
                mark = "✅" if outcome == "success" else "❌"
                print(f"{mark} {label}: {part.get('tool_name')} [{outcome}]")
                print(f"      {_clip(part.get('content', ''), full, 400)}")
            else:
                content = part.get("content", "")
                print(f"{icon} {label}: {_clip(content, full)}")
            print()


def follow(key: str | None) -> None:
    """Tail the live run log written by ``RunLogCapability``, formatted.

    The session file only appears when a turn *ends*, so `trace.py <key>` cannot
    answer "where is it right now". This can: the log is appended per tool call.
    Without a key, the most recently written log is followed.
    """
    directory = Path(RUNLOG_DIR)
    if key:
        path = directory / f"{key}.jsonl"
    else:
        logs = sorted(directory.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not logs:
            print(f"Kein Protokoll in {RUNLOG_DIR}/ — läuft gerade etwas?")
            sys.exit(1)
        path = logs[0]
    print(f"[{path.name}]  Strg-C beendet\n")
    with path.open(encoding="utf-8") as fh:
        while True:
            line = fh.readline()
            if not line:
                time.sleep(0.5)
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue  # halb geschriebene letzte Zeile — beim nächsten Mal ganz da
            if r.get("kind") == "text":
                # `repeats` flags a degenerate reply at a glance: healthy answers sit
                # at 1-2, a model looping on one line runs into the hundreds.
                flag = "  ⚠ REPETITION" if r.get("repeats", 0) > 5 else ""
                print(f"{r.get('t','')[11:]} 💬 {'':<22}{r.get('chars',0):>6} Z."
                      f"  {r.get('text','')[:100]}{flag}")
                continue
            body = r.get("args") or r.get("result") or r.get("error") or ""
            secs = f"{r['seconds']:>6.1f}s" if r.get("seconds") is not None else " " * 7
            arrow = {"call": "→", "result": "←", "error": "✗",
                     "invalid": "⊘"}.get(r.get("kind"), " ")
            print(f"{r.get('t','')[11:]} {arrow} {r.get('tool',''):<22}{secs}  {body[:110]}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = {a for a in sys.argv[1:] if a.startswith("-")}
    if args and args[0] == "live":
        follow(args[1] if len(args) > 1 else None)
        return
    if not args:
        list_sessions()
        return

    key = args[0]
    if key == "last":
        files = session_files()
        if not files:
            print(f"No sessions in {SESSIONS_DIR}/")
            sys.exit(1)
        key = files[0].stem

    show(key, full="--full" in flags, show_system="--system" in flags)


if __name__ == "__main__":
    main()