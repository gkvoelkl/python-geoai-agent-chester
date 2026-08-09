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
from datetime import datetime
from pathlib import Path

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
        return json.loads(meta_path.read_text(encoding="utf-8")).get("last_system_prompt")
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

    messages = json.loads(path.read_text(encoding="utf-8"))
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


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = {a for a in sys.argv[1:] if a.startswith("-")}
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
