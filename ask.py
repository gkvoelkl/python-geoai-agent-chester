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

from dotenv import load_dotenv
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from selmakit import Gateway

from agent_build import (
    CONFIG_NAME,
    STATE_DIR,
    geo_capabilities,
    register_validation_gate,
)
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
) -> None:
    """Send one prompt and stream the response.

    With ``show_tools`` the agent↔LLM tool exchange is streamed too: each tool
    call with its arguments and each result (both truncated), so a run can be
    followed live instead of only seeing the final answer.

    By default each chunk is printed to stdout (the CLI). Pass ``sink`` — a
    callable taking the already-formatted string — to redirect the same stream
    elsewhere (e.g. a Streamlit placeholder for live output in the web bench);
    the event handling is identical, so terminal and UI can't drift.
    """
    def emit(chunk: str, end: str = "\n") -> None:
        if sink is not None:
            sink(chunk + end)
        else:
            print(chunk, end=end, flush=True)

    async with agent.run_stream_events(prompt, session_key=session_key) as (
        is_cmd,
        value,
    ):
        if is_cmd:
            emit(str(value))
            return
        # A single bad tool call (e.g. the model exhausting a tool's retries)
        # raises out of the stream; catch it so one failure emits a clean line
        # instead of crashing the whole run with a traceback.
        try:
            async for event in value:
                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                    if event.part.content:
                        emit(event.part.content, end="")
                elif isinstance(event, PartDeltaEvent) and isinstance(
                    event.delta, TextPartDelta
                ):
                    if event.delta.content_delta:
                        emit(event.delta.content_delta, end="")
                elif isinstance(event, FunctionToolCallEvent):
                    if show_tools:
                        args = _fmt_json(event.part.args, _MAX_ARGS_CHARS)
                        emit(f"\n→ {event.part.tool_name}({args})")
                    else:
                        emit(f"\n[tool: {event.part.tool_name}]")
                elif isinstance(event, FunctionToolResultEvent) and show_tools:
                    result = _fmt_json(event.part.content, _MAX_RESULT_CHARS)
                    emit(f"← {event.part.tool_name}: {result}")
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - one run must not take down the CLI
            emit(f"\n[run error: {type(exc).__name__}: {exc}]")
    emit("")


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
    agent = Gateway.from_config(
        STATE_DIR, CONFIG_NAME, extra_capabilities=geo_capabilities()
    ).agent
    # The enforcing validation gate applies to the CLI too (so benchmarks and
    # scripted runs share the same loop phase as the web channel); /valid_level is
    # web-only, so the CLI just uses the default level (1).
    register_validation_gate(agent)
    if len(sys.argv) > 1:
        asyncio.run(ask(agent, " ".join(sys.argv[1:])))
    else:
        asyncio.run(interactive(agent))


if __name__ == "__main__":
    main()
