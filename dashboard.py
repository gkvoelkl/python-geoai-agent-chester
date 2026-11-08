"""Chester dashboard — Streamlit web UI.

A thin frontend over SelmaKit's reusable dashboard. It talks to the running
gateway (``gateway.py``) over its SSE endpoints; ``selmakit.dashboard.run``
renders the whole app, so this file is just branding + wiring.

Run the gateway first (``uv run gateway.py``), then this dashboard
(``uv run streamlit run dashboard.py``) — or use ``./start.sh`` for both.
"""

import os

from selmakit.dashboard import run

from agent_build import CONFIG_NAME, STATE_DIR
from planpanel import plan_panel

# `start_team.sh` / `start_code.sh` point the same dashboard at their own gateway's port.
_TITLES = {"team": "🌍 Chester-Team — Orchestrator",
           "code": "🌍 Chester-Code — one program per task"}
_KIND = os.environ.get("CHESTER_AGENT", "").strip().lower()

run(
    title=_TITLES.get(_KIND, "🌍 Chester — Geo-AI Agent"),
    image="chester.png",
    input_placeholder="Ask Chester about geospatial data…",
    # The settings dialog edits Chester's config, not selmakit.json.
    config_file=f"{STATE_DIR}/{CONFIG_NAME}",
    # The agent's plan as state, not as one tool call among dozens (see planpanel.py).
    sidebar_panels=[plan_panel],
    # No client-side read timeout: QGIS/STAC turns can run for minutes with no
    # SSE event. The gateway is the authority — it ends idle streams after
    # model.timeout_seconds + 10s.
    stream_timeout=None,
    # Defaults to :8000 (webchat); team and coder pass their own gateway's port in.
    gateway_base_url=os.environ.get("CHESTER_GATEWAY_URL", "http://localhost:8000"),
)
