"""One place to build the agent under test — chester-agent, chester-team or chester-code.

The bench, the CLIs and the probes all need "the agent, wired like the product": the
capability set, then the enforcing gate. That was six identical blocks (`ask.py`,
`probe.py`, `dialog.py`, `evals.py`, `testprompt.py`, `test_app.py`), and the drift the
structure tests hunt — a builder that forgets the capability filter or the gate — could
happen in any of them. Now it can happen in one.

``CHESTER_AGENT=team`` / ``=code`` builds chester-team's orchestrator or chester-code's
coder instead (`chester.evalcells.agent_kind` writes the kind into every history record,
so the architectures never average together).
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
    if kind in ("team", "code"):  # sibling architectures, each with gate and commands
        from chester.code.coder import build_code_gateway
        from chester.team.orchestrator import build_team_gateway

        return (build_team_gateway if kind == "team" else build_code_gateway)(config_name).agent
    # The run's config travels with it: under a side config (`testprompt.py --model`) the
    # prompt cache, token limit and visual check read the model named *there* — the main
    # one would silently drop `max_tokens` on a hosted run (walkthrough station 4).
    agent = Gateway.from_config(
        STATE_DIR,
        config_name,
        capabilities=selmakit_capabilities,
        extra_capabilities=geo_capabilities(config_name=config_name),
    ).agent
    register_validation_gate(agent, config_name=config_name)
    return agent
