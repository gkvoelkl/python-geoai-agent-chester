"""Chester-Code — command-line chat against the coder of chester-code.

The counterpart of `ask.py` for the coder: the same streaming, the same validation
gate, but the agent's only tool is `run_code`, in which it writes one Python program
per task against all geo tools (`chester.code`).

Usage:
    uv run ask_code.py "your prompt"     # one-shot, prints the answer and exits
    uv run ask_code.py                   # interactive chat (Ctrl-D / 'exit' to quit)
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from agents import build_agent
from ask import ask, interactive
from setup import setup


def main() -> None:
    load_dotenv()  # hosted-provider keys (ANTHROPIC_API_KEY, …) from a local .env
    setup(quiet=True)
    agent = build_agent("code")  # the coder, gate and commands included
    if len(sys.argv) > 1:
        asyncio.run(ask(agent, " ".join(sys.argv[1:]), session_key="code-cli"))
    else:
        asyncio.run(interactive(agent))


if __name__ == "__main__":
    main()
