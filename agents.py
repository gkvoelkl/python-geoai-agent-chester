"""One place to build the agent under test — chester-agent or chester-team.

The bench, the CLIs and the probes all need "the agent, wired like the product":
the capability set, then the enforcing gate. That was six identical blocks
(`ask.py`, `probe.py`, `dialog.py`, `evals.py`, `testprompt.py`, `test_app.py`), and
the drift the structure tests hunt — a builder that forgets the capability filter or
the gate — could happen in any of them. Now it can happen in one.

``CHESTER_AGENT=team`` builds chester-team's orchestrator instead (see
`chester.evalcells.agent_kind`, which also writes the kind into every history
record, so the two architectures never average together).
"""

from __future__ import annotations

from selmakit import Gateway

from agent_build import (
    CONFIG_NAME,
    STATE_DIR,
    geo_capabilities,
    register_validation_gate,
    selmakit_capabilities,
)
from chester.evalcells import agent_kind


def build_agent(kind: str | None = None, config_name: str = CONFIG_NAME):
    """The agent under test with the product's wiring (capability set + gate)."""
    kind = kind or agent_kind()
    if kind == "team":
        from chester.team.orchestrator import build_team_gateway

        return build_team_gateway(config_name).agent  # gate and commands included
    agent = Gateway.from_config(
        STATE_DIR,
        config_name,
        capabilities=selmakit_capabilities,
        extra_capabilities=geo_capabilities(),
    ).agent
    register_validation_gate(agent)
    return agent
