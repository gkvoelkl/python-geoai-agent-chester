"""Chester-Code gateway — the coder behind web chat + SSE, beside the agent's and the team's.

The counterpart of `gateway.py`. It serves on the coder's own port (``code.webchat_port``
in `.chester/chester.json`, default 8200) and never starts Telegram, so it can run next
to `gateway.py` and `gateway_team.py`: `chester.code.coder.effective_config` derives the
coder's config from the main one on every start.

Usage:
    uv run gateway_code.py        # usually via ./start_code.sh
"""

from dotenv import load_dotenv

from agent_build import start_geocache_sync
from chester.code.coder import build_code_gateway
from setup import setup


def main() -> None:
    load_dotenv()
    setup(quiet=True)
    gateway = build_code_gateway()  # gate and the data commands included
    start_geocache_sync()
    gateway.run()


if __name__ == "__main__":
    main()
