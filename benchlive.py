"""benchlive.py — one timed timeline for a bench run, live from the first token.

The Run tab used to show the same run twice: a timestamped text log (live, but
unstructured) and SelmaKit's transcript (structured — model input, model output,
tool call↔return pairs — but only after the turn, because the assembled
instructions and a reliable pairing live in the session file, not in the stream).
Each answered half of the question, and neither on its own said *when*.

This module merges them:

- **During the run** every stream event becomes a SelmaKit transcript ``Row``
  (``ask(..., on_event=...)`` hands them over structurally, untruncated), and each
  row carries the wall-clock time plus the gap to the row before it — the column
  the transcript never had. A tool row's stamp turns into that tool's duration
  once its result arrives, so a slow step reads as a number.
- **After the turn** the session file adds what no stream carries: the
  SYSTEM/CONTEXT rows, i.e. what the model was actually given. They are prepended;
  the rows of the streamed turn stay exactly as captured, so no timestamp is
  reconstructed by matching persisted parts back against the stream.

Rows are built with SelmaKit's own row helpers and drawn with its row renderer —
the time column is one more grid column around it, never a second copy of the
renderer. If a future SelmaKit drops those internals the view falls back to the
plain transcript rather than to a stale fork.
"""

from __future__ import annotations

import contextlib
import json
import time
from datetime import datetime
from pathlib import Path

import streamlit as st
from selmakit.dashboard import transcript as tv

# The time column: same grid as SelmaKit's transcript, one column wider.
_CSS = """
<style>
.sk-transcript.ck-timed { grid-template-columns: 76px 54px 92px minmax(0, 1fr); }
.ck-time {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.66rem; line-height: 1.35; opacity: 0.55; white-space: nowrap;
}
.ck-span { opacity: 0.75; }
</style>
"""


def _fmt(value, limit: int = 4000) -> str:
    """Tool args/results as a string — JSON when they are structured."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}… (+{len(text) - limit} chars)"


def _stamp(clock: str, span: str = "") -> str:
    """One time cell: wall clock, and below it the gap or a tool's duration."""
    return f"{clock}<br><span class='ck-span'>{span}</span>" if span else clock


def render(rows: list, times: list[str]) -> None:
    """Draw rows with their time cells (``times`` may be shorter — pads with blanks)."""
    if not rows:
        st.caption("Noch nichts gelaufen.")
        return
    row_html = getattr(tv, "_row_html", None)
    css = getattr(tv, "_CSS", "")
    if row_html is None:  # SelmaKit changed its internals — transcript without times
        tv.render_transcript(rows)
        return
    cells = list(times) + [""] * (len(rows) - len(times))
    body = "".join(
        f'<div class="ck-time">{cell}</div>{row_html(row)}'
        for row, cell in zip(rows, cells, strict=False)
    )
    st.html(f'{css}{_CSS}<div class="sk-transcript ck-timed">{body}</div>')


class LiveRun:
    """The running turn as timed transcript rows, painted into a placeholder.

    Fed by ``ask``'s ``on_event``. Painting is throttled: text arrives token by
    token, and redrawing the whole grid per token would spend the run's time on
    rendering instead of on the agent.
    """

    def __init__(self, placeholder, *, throttle: float = 0.4) -> None:
        self.rows: list = []
        self.times: list[str] = []
        self._box = placeholder
        self._throttle = throttle
        self._last = time.monotonic()  # end of the previous row → gap of the next
        self._painted = 0.0
        self._text = ""  # the answer block currently being streamed
        self._calls: list[tuple[int, str, str, float]] = []  # open tool calls

    # -- feeding ---------------------------------------------------------------

    def start(self, prompt: str) -> None:
        """Open the turn with the prompt, so the view starts at the model's input."""
        self._append(tv.text_row("USER", prompt))
        self.paint(force=True)

    def on_event(self, kind: str, fields: dict) -> None:
        if kind in ("text", "thinking"):
            self._grow("ASSISTANT" if kind == "text" else "THINKING", fields.get("text", ""))
        elif kind == "tool_call":
            self._text = ""  # a tool call closes the current answer block
            args = _fmt(fields.get("args"))
            name = str(fields.get("name", "tool"))
            self._append(tv.tool_row(name, args))
            self._calls.append((len(self.rows) - 1, name, args, time.monotonic()))
        elif kind == "tool_result":
            self._fold_result(fields)
        self.paint()

    def _grow(self, kind: str, chunk: str) -> None:
        """Extend the trailing prose row of that kind, or open a new one.

        Reasoning and answer are both streamed in fragments; the row keeps the time
        it *started* at, which is the number that says how long the model has been
        at it. A row of the other kind in between closes the block.
        """
        self._text = self._text + chunk if self.rows and self.rows[-1].kind == kind else chunk
        row = tv.text_row(kind, self._text)
        if self.rows and self.rows[-1].kind == kind:
            self.rows[-1] = row
        else:
            self._append(row)

    def _fold_result(self, fields: dict) -> None:
        """Merge a result into its call row — and stamp that row with the duration."""
        name = str(fields.get("name", "tool"))
        for i in range(len(self._calls) - 1, -1, -1):
            index, called, args, started = self._calls[i]
            if called != name:
                continue
            self._calls.pop(i)
            self.rows[index] = tv.tool_row(
                name, args, _fmt(fields.get("result")), error=bool(fields.get("error"))
            )
            clock = self.times[index].split("<br>")[0]
            self.times[index] = _stamp(clock, f"⏱{time.monotonic() - started:.1f}s")
            return

    def _append(self, row) -> None:
        now = time.monotonic()
        gap = now - self._last
        self._last = now
        # "+0.0s" on a burst of tokens is noise; only a real wait gets a number —
        # the same rule `testprompt.timestamped_sink` applies to the text log.
        span = f"+{gap:.1f}s" if gap >= 0.1 else ""
        self.rows.append(row)
        self.times.append(_stamp(datetime.now().strftime("%H:%M:%S"), span))

    # -- painting --------------------------------------------------------------

    def paint(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._painted < self._throttle:
            return
        self._painted = now
        with self._box.container():
            render(self.rows, self.times)


def merged(sessions_dir: str, session_key: str, rows: list, times: list[str]) -> tuple[list, list]:
    """Persisted model input + the timed rows of the streamed turn.

    From the session file only what the stream cannot carry is taken: everything
    before the last turn's first message — which is exactly the instructions block
    (SYSTEM/CONTEXT rows, emitted ahead of the turn's USER row) plus any earlier
    turn of the same session. Falls back to the live rows alone when there is no
    session file, and to the persisted rows alone when there was no live capture
    (an old run reopened after a rerun).
    """
    messages = tv.load_session_messages(sessions_dir, session_key)
    persisted = tv.build_rows(messages) if messages else []
    if not rows:
        return persisted, []
    starts = [i for i, row in enumerate(persisted) if row.turn]
    head = persisted[: starts[-1]] if starts else persisted
    return head + rows, [""] * len(head) + list(times)


# ── a past run: the kept protocol and the trace copy beside it ────────────────


def _rows_per_message(message: dict, seen: list[str]) -> int:
    """How many rows ``build_rows`` emits for this message.

    Mirrors its two rules — a new instructions blob becomes one row per section,
    and a tool return is folded into the call row rather than becoming its own —
    because the row list itself does not say which message a row came from. The
    caller checks the total against the real row count and drops the time column
    when the two disagree, so a future part kind costs a column, never a wrong time.
    """
    count = 0
    instructions = message.get("instructions") or ""
    if instructions and instructions != seen[0]:
        seen[0] = instructions
        count += len(tv._split_instructions(instructions))
    return count + sum(
        1
        for part in message.get("parts", [])
        if part.get("part_kind") not in ("tool-return", "retry-prompt")
    )


def _reinjection_row(before: str, after: str):
    """One row standing in for an instructions blob the agent sent again.

    A long run re-sends the whole blob whenever any part of it changes — in a
    26-minute run that was 25 blocks of 23 sections, two thirds of every row on
    screen. Repeating them hides the run; naming the sections that actually
    differ shows *why* it was re-sent (and why the model reprocessed the prompt).
    """

    # `_split_instructions` yields (row kind, "Heading — body"), so the section's
    # name has to be taken off the front of the text, not out of the first field.
    # The SYSTEM entry is skipped: when a capability has no heading its text is
    # "Initial system prompt · N chars", so it "changes" whenever anything else does.
    def by_heading(blob: str) -> dict[str, str]:
        return {
            text.split(" — ")[0]: text
            for kind, text in tv._split_instructions(blob)
            if kind != "SYSTEM"
        }

    old, new = by_heading(before), by_heading(after)
    changed = [head for head, text in new.items() if old.get(head) != text]
    what = ", ".join(changed) if changed else "nichts Sichtbares"
    return tv.text_row(
        "CONTEXT",
        f"Instruktionen erneut gesendet · {len(new)} Abschnitte · geändert: {what}",
    )


def timed_rows(messages: list[dict], *, collapse_repeats: bool = True) -> tuple[list, list[str]]:
    """A persisted run as rows, timed from the messages' own timestamps.

    The stream's per-row clock is gone once a run is over; what survives in the
    session file is one timestamp per message. That is enough for the column that
    matters — the gap in front of a message is the wait it ended.

    ``collapse_repeats`` folds every instructions block after the first into a
    single row; pass ``False`` to see the model's input verbatim, block by block.
    """
    rows = tv.build_rows(messages)
    stamps = [""] * len(rows)
    seen = [""]
    previous = None
    cursor = 0
    folded: list[tuple[int, int, object]] = []  # (start, length, replacement row)
    for message in messages:
        was = seen[0]
        count = _rows_per_message(message, seen)
        if collapse_repeats and was and seen[0] != was:
            block = len(tv._split_instructions(seen[0]))
            folded.append((cursor, block, _reinjection_row(was, seen[0])))
        at = None
        # A message without a usable timestamp simply has no time cell.
        with contextlib.suppress(ValueError, TypeError):
            at = datetime.fromisoformat(str(message.get("timestamp", "")).replace("Z", "+00:00"))
        gap = None if at is None or previous is None else (at - previous).total_seconds()
        # A message that carries only tool returns owns no row; it still moves the
        # clock, so the wait it ends shows up on the row after it. Its gap is *not*
        # the tool's runtime — a response is timestamped when it began, so the span
        # up to the return holds generation and tool execution in one number, and
        # the messages cannot separate them. Only the raw protocol below can (the
        # live view can, because it sees call and return as they happen).
        if count and at is not None:
            stamps[cursor] = _stamp(
                at.astimezone().strftime("%H:%M:%S"), "" if gap is None else f"+{gap:.1f}s"
            )
        previous = at or previous
        cursor += count
    if cursor != len(rows):  # row↔message mapping broke: no times, no folding
        return rows, []
    for start, length, replacement in reversed(folded):  # from the back: indices hold
        rows[start : start + length] = [replacement]
        stamps[start : start + length] = [stamps[start]]
    return rows, stamps


def run_logs(runs_dir: str | Path) -> list[Path]:
    """Every kept run protocol, newest first."""
    return sorted(Path(runs_dir).glob("*.log"), reverse=True)


def log_for(runs_dir: str | Path, record: dict) -> Path | None:
    """The protocol belonging to a judged run.

    Records written before the protocol existed have no ``log`` field, so fall back
    to the newest file for that test that started *before* the run was archived —
    the archive timestamp is taken after the run, never before it.
    """
    named = record.get("log")
    if named and Path(named).exists():
        return Path(named)
    test_id, ts = record.get("test_id", ""), str(record.get("ts", ""))
    stamp = ts.replace("-", "").replace(":", "")[:15]
    candidates = [p for p in run_logs(runs_dir) if p.stem.endswith(f"__{test_id}")]
    earlier = [p for p in candidates if p.stem[:15] <= stamp]
    return earlier[0] if earlier else None


def render_past_run(log_path: str | Path) -> None:
    """A kept run: its trace copy as a timed transcript, the raw protocol below.

    The trace copy is what makes this work at all — the live session file belongs to
    whichever run went last, so a run from yesterday can only be read back from the
    ``.trace.json`` saved beside its protocol.
    """
    path = Path(log_path)
    if not path.exists():
        st.caption(f"Protokoll nicht gefunden: `{path}`")
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    header, _, body = text.partition("\n\n")
    st.code(header, language="yaml")

    trace = path.with_suffix("").with_suffix(".trace.json")
    if trace.exists():
        try:
            messages = json.loads(trace.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            messages = []
        if messages:
            rows, times = timed_rows(messages)
            render(rows, times)
            if not times:
                st.caption("Ohne Zeitspalte: die Nachrichten tragen keine brauchbaren Zeiten.")
    else:
        st.caption("Keine Trace-Kopie neben diesem Protokoll — nur das Rohprotokoll unten.")

    st.markdown(f"**Rohprotokoll** — `{path}`")
    st.code(body or text)
