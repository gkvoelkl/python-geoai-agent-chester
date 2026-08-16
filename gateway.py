"""Chester gateway — the agent backend (web chat + SSE).

Chester is SelmaKit's default capabilities plus its geo capabilities; everything
else (model, session store, memory, cron, channels) comes from
``Gateway.from_config`` reading ``.chester/chester.json``. So this entry point is
the SelmaKit reference gateway with two Chester touches: a custom config
location and the geo capabilities via ``extra_capabilities``.

Tracing: off unless ``.chester/chester.json`` carries a ``tracing`` block with
``enabled: true`` (since selmakit 0.1.26 — before that ``serve()`` exported
unconditionally and every turn retried a refused connection). Point ``endpoint``
at any OTLP/HTTP collector. For reading a run without one, use the dashboard's
Transcript view, or ``trace.py``, which reads the per-session trace SelmaKit
persists under ``.chester/sessions/``.

Usage:
    uv run gateway.py        # serves on the configured host:port (default :8000)
"""

from dotenv import load_dotenv
from selmakit import Gateway

from agent_build import (
    CONFIG_NAME,
    STATE_DIR,
    geo_capabilities,
    register_geo_commands,
    register_validation_gate,
    start_geocache_sync,
)
from setup import setup


def main() -> None:
    load_dotenv()  # hosted-provider keys (ANTHROPIC_API_KEY, …) from a local .env
    setup(quiet=True)  # ensure .chester config + workspace identity + deployed skills
    gateway = Gateway.from_config(STATE_DIR, CONFIG_NAME, extra_capabilities=geo_capabilities())
    # Chester's channel-intercepted slash commands (/geocache, /geoconnector,
    # /geodataset, /valid_level, …) — registered on the agent per SelmaKit's
    # documented pattern.
    register_geo_commands(gateway.agent)
    # The enforcing validation gate (result-based output_validator, /valid_level).
    register_validation_gate(gateway.agent)
    # Keep the GeoCache bounded on a long-running gateway (opt-in via
    # geodata.sync_interval_hours; a daemon thread, so no shutdown handling).
    start_geocache_sync()
    gateway.run()


if __name__ == "__main__":
    main()
