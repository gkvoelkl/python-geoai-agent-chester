"""The dashboard's plan panel — the agent's current plan as *state*, not events.

`Planning` (wired in ``agent_build.geo_capabilities``) makes the model call
``write_plan`` with the complete plan each time it changes. In the chat stream that
shows up as one more tool call among dozens, so the same checklist appears five
times in a row, slightly different, and the current one is the entry furthest down —
exactly where nobody is looking. This renders the latest one in the sidebar instead,
where it overwrites itself.

Deliberately a **view over data already flowing**: SelmaKit's dashboard hands every
panel the turn's tool activity (``SidebarContext``), so there is no second source of
truth and no way for the display to disagree with the run. The plan lives in the
gateway process; this reads what came out of it.

Not in ``chester/`` on purpose — the pure cores import no SelmaKit and no Streamlit.
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

#: Status → marker. Mirrors the harness's own `TaskStatus`, spelled out here rather
#: than imported from `pydantic_ai_harness.planning._toolset._ICONS`: that name is
#: private, and a rendering choice is ours to make anyway.
_MARKERS = {
    "pending": "○",
    "in_progress": "▶",
    "completed": "✓",
    "cancelled": "✗",
    "blocked": "⏸",
}


def _as_dict(args: Any) -> dict:
    """A tool call's arguments as a dict — they arrive as JSON text or already parsed."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _latest_plan(entries) -> list[dict]:
    """The items of the last ``write_plan`` call in ``entries`` (empty if none)."""
    for entry in reversed(list(entries or ())):
        if entry.get("kind") != "call" or entry.get("name") != "write_plan":
            continue
        items = _as_dict(entry.get("args")).get("items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
    return []


def _fallback_entries(messages) -> tuple:
    """The last *assistant* turn's activity, for the gaps between turns.

    Assistant is not simply the last message: the history also carries ``cron`` and
    ``notification`` roles, and taking the last one would blank the panel whenever
    a notification arrives.
    """
    for message in reversed(list(messages or ())):
        if message.get("role") == "assistant" and message.get("tool_activity"):
            return tuple(message["tool_activity"])
    return ()


def plan_panel(ctx) -> None:
    """Render the current plan into the sidebar. Silent when there is none.

    A run without a plan is normal — a one-step question needs none — so an empty
    plan draws nothing rather than an empty box.
    """
    items = _latest_plan(ctx.tool_activity) or _latest_plan(_fallback_entries(ctx.messages))
    if not items:
        return

    done = sum(1 for i in items if i.get("status") == "completed")
    st.markdown(f"**Plan** · {done}/{len(items)}")

    lines = []
    for item in items:
        status = str(item.get("status", "pending"))
        marker = _MARKERS.get(status, "•")
        # `active_form` is the step phrased as an activity ("Fetching the DEM"); it
        # reads better than the imperative `content` for the step being worked on.
        text = (item.get("active_form") if status == "in_progress" else None) or item.get(
            "content", ""
        )
        if status == "in_progress":
            lines.append(f"{marker} **{text}**")
        elif status in ("completed", "cancelled"):
            lines.append(f"<span style='opacity:.55'>{marker} {text}</span>")
        else:
            lines.append(f"{marker} {text}")
    st.markdown(
        "<div style='font-size:.85em;line-height:1.6'>" + "<br>".join(lines) + "</div>",
        unsafe_allow_html=True,
    )
