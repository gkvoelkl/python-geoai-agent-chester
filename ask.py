"""Chester — command-line chat against the agent.

A slim CLI that reuses the gateway's wiring without the web stack: it builds the
same agent via ``Gateway.from_config(...).agent`` (no channels are started) and
streams replies to stdout. The gateway/dashboard remain the primary interface;
this is for one-shot scripting and terminal use.

Usage:
    uv run ask.py "your prompt"     # one-shot, prints the answer and exits
    uv run ask.py                   # interactive chat (Ctrl-D / 'exit' to quit)
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter

from dotenv import load_dotenv
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.run import AgentRunResultEvent

from agents import build_agent
from chester.runtime import live
from setup import setup

# When streaming the agent↔LLM exchange (``show_tools``), truncate the noisy
# parts so a big tool result (e.g. a 44k-feature GeoJSON echo) can't flood the
# console; the head is enough to eyeball what happened.
_MAX_ARGS_CHARS = 600
_MAX_RESULT_CHARS = 800


def _truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars, noting how much was dropped."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (+{len(text) - limit} chars)"


def _ressort_line(tool_name: str | None, content) -> str:
    """One extra line under a ressort's return: which tools it used inside, and how it
    ended. With chester-team the visible calls are ressort names — what actually
    happened is a level below, and a truncated JSON blob is not where one reads it
    (added 2026-09-20, after a ressort called the same wrong tool 22 times)."""
    if not (tool_name or "").startswith("ressort_") or not isinstance(content, dict):
        return ""
    # With the count, because a repeat is the finding: one ressort called the same
    # wrong tool 22 times before its cap stopped it, and a deduplicated list hides it.
    counts = Counter(str(t) for t in content.get("tools_called") or [])
    used = ", ".join(f"{name}×{n}" if n > 1 else name for name, n in counts.items())
    state = "ok" if content.get("ok") else (content.get("cap") or content.get("error") or "failed")
    seconds = content.get("duration_s")
    took = f" · {seconds:.0f}s" if isinstance(seconds, (int, float)) else ""
    return f"\n   ↳ {used or 'no tool'}{took} · {state}"


def _fmt_json(value, limit: int) -> str:
    """Render tool args/results as compact JSON (falling back to ``str``)."""
    if isinstance(value, str):
        return _truncate(value, limit)
    try:
        rendered = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    return _truncate(rendered, limit)


async def ask(  # noqa: C901
    # C901-Ausnahme: Ereignisschleife ueber die Agent-Stream-Typen; jeder Zweig ein Ereignistyp
    agent,
    prompt: str,
    session_key: str = "cli",
    show_tools: bool = False,
    sink=None,
    on_event=None,
) -> str | None:
    """Send one prompt, stream the response, and return the **validated** answer.

    Zurück kommt, was der Aufrufer wirklich bekommt — mit der Anmerkung des
    Validierungs-Gates, die ein ``output_validator`` an das Ergebnis hängt und die
    in den gespeicherten Nachrichten **nicht** steht (SelmaKit sichert sie vor dem
    Validator). Gelesen wird sie aus dem ``AgentRunResultEvent``; fällt das aus,
    bleibt der aus dem Stream mitgeschnittene Modelltext.

    Die Stelle hat drei Fassungen gebraucht, das gehört dazu: Am 2026-08-27 als
    „liefert die validierte Antwort" geschrieben und nur auf Unit-Ebene geprüft; am
    2026-08-30 im ersten Dialoglauf widerlegt — SelmaKit 0.1.32 fing das Ereignis in
    ``run_stream_events`` ab und reichte es nicht weiter, ``ask()`` gab also immer
    ``None`` zurück und jede Gate-Meldung blieb für Protokoll, Trace und Judge
    unsichtbar. Behoben stromaufwärts in **SelmaKit 0.1.33** (Ereignis wird
    weitergereicht) und mit einem echten Lauf nachgewiesen
    (``tests/test_judge_guards.py::test_ask_returns_the_validated_answer``).

    ``None`` nur, wenn der Zug gar nichts produziert hat (Slash-Befehl, oder ein
    Lauf, der im Stream starb).

    With ``show_tools`` the agent↔LLM tool exchange is streamed too: each tool
    call with its arguments and each result (both truncated), so a run can be
    followed live instead of only seeing the final answer.

    By default each chunk is printed to stdout (the CLI). Pass ``sink`` — a
    callable taking the already-formatted string — to redirect the same stream
    elsewhere (e.g. a Streamlit placeholder for live output in the web bench);
    the event handling is identical, so terminal and UI can't drift.

    ``on_event`` gets the same events *structurally* — ``("text" | "tool_call" |
    "tool_result", fields)`` with untruncated values — for a consumer that renders
    rows rather than lines (``benchlive``'s live transcript). Formatting stays here,
    so there is still exactly one event loop.
    """

    def emit(chunk: str, end: str = "\n") -> None:
        if sink is not None:
            sink(chunk + end)
        else:
            print(chunk, end=end, flush=True)

    def note(kind: str, **fields) -> None:
        if on_event is not None:
            on_event(kind, fields)

    # With chester-team the work happens *inside* a tool call (`ressort_vector` runs an
    # agent of its own, for minutes). Publishing this stream lets whoever runs in there
    # write its own calls into it as they happen — nothing does when no team is running.
    live_sink = (lambda chunk: emit(chunk, end="")) if show_tools else None
    with live.use_sink(live_sink):
        return await _stream(agent, prompt, session_key, show_tools, emit, note)


async def _stream(agent, prompt, session_key, show_tools, emit, note):  # noqa: C901, PLR0913
    # C901/PLR0913 exception: the event loop over the agent stream — one branch per
    # event type, and it carries the run plus its two output channels.
    final_output: str | None = None
    text_parts: list[str] = []
    async with agent.run_stream_events(prompt, session_key=session_key) as (
        is_cmd,
        value,
    ):
        if is_cmd:
            emit(str(value))
            return None
        # A single bad tool call (e.g. the model exhausting a tool's retries)
        # raises out of the stream; catch it so one failure emits a clean line
        # instead of crashing the whole run with a traceback.
        try:
            async for event in value:
                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                    if event.part.content:
                        emit(event.part.content, end="")
                        note("text", text=event.part.content)
                        text_parts.append(event.part.content)
                elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                    if event.delta.content_delta:
                        emit(event.delta.content_delta, end="")
                        note("text", text=event.delta.content_delta)
                        text_parts.append(event.delta.content_delta)
                # Reasoning goes to the structured consumer only, never to `emit`: on a
                # local reasoning model it is where minutes disappear, so a live view
                # must show it — while the terminal protocol stays what it always was.
                elif isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart):
                    note("thinking", text=event.part.content)
                elif isinstance(event, PartDeltaEvent) and isinstance(
                    event.delta, ThinkingPartDelta
                ):
                    note("thinking", text=event.delta.content_delta or "")
                elif isinstance(event, FunctionToolCallEvent):
                    note("tool_call", name=event.part.tool_name, args=event.part.args)
                    if show_tools:
                        args = _fmt_json(event.part.args, _MAX_ARGS_CHARS)
                        emit(f"\n→ {event.part.tool_name}({args})")
                    else:
                        emit(f"\n[tool: {event.part.tool_name}]")
                elif isinstance(event, FunctionToolResultEvent):
                    # A retry prompt is a tool call's failure channel — same event,
                    # so the live row can mark it instead of showing a plain result.
                    note(
                        "tool_result",
                        name=event.part.tool_name,
                        result=event.part.content,
                        error=getattr(event.part, "part_kind", "") == "retry-prompt",
                    )
                    if show_tools:
                        result = _fmt_json(event.part.content, _MAX_RESULT_CHARS)
                        emit(f"← {event.part.tool_name}: {result}")
                        emit(_ressort_line(event.part.tool_name, event.part.content))
                elif isinstance(event, AgentRunResultEvent):
                    # Das Ende des Laufs trägt die *validierte* Ausgabe — die einzige
                    # Stelle, an der die angehängte Gate-Notiz zu lesen ist. Seit
                    # SelmaKit 0.1.33 kommt das Ereignis hier an.
                    final = getattr(event.result, "output", None)
                    if isinstance(final, str):
                        final_output = final
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - one run must not take down the CLI
            emit(f"\n[run error: {type(exc).__name__}: {exc}]")
    emit("")
    # Der Ergebnis-Zweig gewinnt, wenn er je feuert; sonst der mitgeschnittene Text.
    return final_output or ("".join(text_parts) or None)


async def interactive(agent) -> None:
    """Run a blocking read-eval chat loop."""
    print("Chester ready. Type 'exit' or Ctrl-D to quit.\n")
    while True:
        try:
            prompt = input("you> ").strip()
        except EOFError:
            print()
            break
        if prompt.lower() in {"exit", "quit"}:
            break
        if not prompt:
            continue
        print("chester> ", end="", flush=True)
        await ask(agent, prompt)


def main() -> None:
    load_dotenv()  # hosted-provider keys (ANTHROPIC_API_KEY, …) from a local .env
    setup(quiet=True)
    # Building the Gateway wires the agent (model, memory, capabilities) without
    # starting any channels; we just borrow its ``.agent`` for terminal use.
    agent = build_agent()
    if len(sys.argv) > 1:
        asyncio.run(ask(agent, " ".join(sys.argv[1:])))
    else:
        asyncio.run(interactive(agent))


if __name__ == "__main__":
    main()
