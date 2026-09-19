"""Chester-Team — command-line chat against the orchestrator of chester-team.

The counterpart of `ask.py` for the multi-agent: the same streaming, the same
validation gate, but the agent is the orchestrator, whose tools are the five
ressorts (`chester.team`). Each ressort call leaves a line in
`.chester/workspace/team-runs/ressort-calls.jsonl`.

Usage:
    uv run ask_team.py "your prompt"     # one-shot, prints the answer and exits
    uv run ask_team.py                   # interactive chat (Ctrl-D / 'exit' to quit)
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
    agent = build_agent("team")  # the orchestrator, gate and commands included
    if len(sys.argv) > 1:
        asyncio.run(ask(agent, " ".join(sys.argv[1:]), session_key="team-cli"))
    else:
        asyncio.run(interactive(agent))


if __name__ == "__main__":
    main()
