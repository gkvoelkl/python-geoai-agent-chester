"""Chester dashboard — Streamlit web UI.

A thin frontend over SelmaKit's reusable dashboard. It talks to the running
gateway (``gateway.py``) over its SSE endpoints; ``selmakit.dashboard.run``
renders the whole app, so this file is just branding + wiring.

Run the gateway first (``uv run gateway.py``), then this dashboard
(``uv run streamlit run dashboard.py``) — or use ``./start.sh`` for both.
"""

from selmakit.dashboard import run

from agent_build import CONFIG_NAME, STATE_DIR

run(
    title="🌍 Chester — Geo-AI Agent",
    image="chester.png",
    input_placeholder="Ask Chester about geospatial data…",
    # The settings dialog edits Chester's config, not selmakit.json.
    config_file=f"{STATE_DIR}/{CONFIG_NAME}",
    # No client-side read timeout: QGIS/STAC turns can run for minutes with no
    # SSE event. The gateway is the authority — it ends idle streams after
    # model.timeout_seconds + 10s.
    stream_timeout=None,
    # gateway_base_url defaults to http://localhost:8000, matching webchat.
)
