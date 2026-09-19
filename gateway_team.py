"""Chester-Team gateway — the orchestrator behind web chat + SSE, beside the agent's.

The counterpart of `gateway.py`. It serves on the team's own port (``team.webchat_port``
in `.chester/chester.json`, default 8100) and never starts Telegram, so it can run
next to `gateway.py`: `chester.team.orchestrator.effective_config` derives the
team's config from the main one on every start.

Usage:
    uv run gateway_team.py        # usually via ./start_team.sh
"""

from dotenv import load_dotenv

from agent_build import start_geocache_sync
from chester.team.orchestrator import build_team_gateway
from setup import setup


def main() -> None:
    load_dotenv()
    setup(quiet=True)
    gateway = build_team_gateway()  # gate and the data commands included
    start_geocache_sync()
    gateway.run()


if __name__ == "__main__":
    main()
