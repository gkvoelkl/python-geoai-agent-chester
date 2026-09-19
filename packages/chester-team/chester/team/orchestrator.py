"""The orchestrator of chester-team — the agent the user talks to.

A SelmaKit agent on chester-runtime, like chester-agent, but with a different tool
surface: instead of 83 geo tools it has **one tool per ressort** plus the check tools.
It splits the task along the chain (find → acquire → compute → present), hands each
piece to a ressort with the paths it needs, and reports the result the last ressort
handed back. The files never pass through it — workspace and GeoCache are the
blackboard, and the ressorts hand over paths (`chester.team.ressorts`).

What it shares with chester-agent comes from chester-runtime, never from
chester-agent: the base capabilities (skill guide, run log, planning with its guard,
prompt cache, model limits, web instruction, tool-output offloading), SelmaKit's
default set minus what Chester never offers, the enforcing gate, the data commands.
The persona is shared too, for now: the same workspace identity files
(decided 2026-09-19, `internal/TODO.md` KP.5 T3); the orchestrator's role comes from
its instructions below.

**The team's own configuration** is the ``team`` block of ``.chester/chester.json``
(decided 2026-09-19). SelmaKit reads its config through ``load_config(state_dir,
config_name)`` only, so :func:`effective_config` writes the main config with the
team's overrides applied — its own web-chat port, Telegram off (two bots cannot share
one token) — to ``.chester/<name>.team.json`` on every start, and the team is built
from that file through the public ``Gateway.from_config``. One source of truth, no
reach into SelmaKit's internals; the derived file is state, rewritten each time.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import ressortcut, wrapperlayer
from chester.runtime import mapinspect
from chester.runtime.config import (
    CONFIG_NAME,
    STATE_DIR,
    WORKSPACE_DIR,
    config_base_url,
    config_main_model,
    config_vision_model,
)
from chester.runtime.mapinspect import build_inspect_map
from chester.runtime.wiring import base_capabilities
from chester.team import ressorts

#: Ports of the team when the ``team`` block names none — clear of the agent's
#: defaults (web chat 8000, dashboard 8501), so both can run side by side.
DEFAULT_WEBCHAT_PORT = 8100
DEFAULT_DASHBOARD_PORT = 8601
DEFAULT_BENCH_PORT = 8602

_INSTRUCTIONS = """\
## You lead a team

You are the orchestrator of a team of ressort agents. You do not touch geodata
yourself: every step of the work goes to the ressort for its phase, and each ressort
hands back the **paths** of what it produced. Geo work is a chain — pass each
ressort the paths from the step before.

- `ressort_scout` — what data exists for the task and where (catalogues, services,
  official boundaries, place names, the local cache). Start here when the source is
  not obvious.
- `ressort_acquisition` — bring data into the cache: official sources, the named
  area rather than a bounding box, converted into a usable layer.
- `ressort_vector` — compute on vector layers; give it the whole chain of vector
  steps in one task (clip → buffer → dissolve is one task, not three).
- `ressort_raster` — compute on rasters and elevation models.
- `ressort_output` — maps and 3D views of a finished result. Never for computing.

Write each task so that the ressort can do it without asking back: the goal, the
input paths, the place, the expected output. A ressort that returns
`capped: true` stopped at a limit — its `report` says what is missing; decide whether
to hand the rest to it again with a narrower task. The check tools are yours as well:
check the final result before you report it, and name the **exact paths** of the
files in your answer.
"""


def ressort_tool(name: str, workspace: str) -> Callable[..., Any]:
    """The tool through which the orchestrator hands one ressort a task."""

    async def call(task: str, input_paths: list[str] | None = None) -> dict:
        return await ressorts.run_ressort(name, task, input_paths, workspace=workspace)

    call.__name__ = f"ressort_{name}"
    call.__doc__ = (
        f"{ressorts._ROLE[name]}\n\nGive it one self-contained task and the input "
        "paths; it returns `outputs` (absolute paths), `report`, `open_points`, and "
        "`capped: true` if it stopped at a limit."
    )
    return call


@dataclass
class OrchestratorCapability(AbstractCapability[Any]):
    """The orchestrator's tools: one per ressort, the check tools, `inspect_map`."""

    workspace: str = WORKSPACE_DIR

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS + "\n" + mapinspect.INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        return FunctionToolset(tools=orchestrator_tools(self.workspace))


def orchestrator_tools(workspace: str) -> list[Callable[..., Any]]:
    """One tool per ressort, then the check tools and the visual check."""
    checks = [t for t in wrapperlayer.collect_tools(workspace)
              if t.__name__ in ressortcut.CHECKS]
    return [
        *(ressort_tool(name, workspace) for name in ressortcut.RESSORTS),
        *checks,
        build_inspect_map(workspace, vision_model=config_vision_model(),
                          base_url=config_base_url(), main_model=config_main_model()),
    ]


def team_capabilities(workspace_dir: str = WORKSPACE_DIR) -> list:
    """The team's capability set: the shared base, then the orchestrator's surface."""
    return [*base_capabilities(workspace_dir), OrchestratorCapability(workspace=workspace_dir)]


def team_block(config: dict | None = None) -> dict:
    """The ``team`` block with its defaults filled in — of ``config`` if given, else of
    the main config file."""
    from chester.runtime.config import config_block

    raw = (config or {}).get("team") if config is not None else config_block("team")
    block = raw if isinstance(raw, dict) else {}
    return {
        "webchat_port": int(block.get("webchat_port") or DEFAULT_WEBCHAT_PORT),
        "dashboard_port": int(block.get("dashboard_port") or DEFAULT_DASHBOARD_PORT),
        "bench_port": int(block.get("bench_port") or DEFAULT_BENCH_PORT),
    }


def effective_config(config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR) -> str:
    """Write the team's effective config next to the main one; return its file name."""
    source = Path(state_dir) / config_name
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    team = team_block(data)
    derived = copy.deepcopy(data)
    channels = derived.setdefault("channels", {})
    channels.setdefault("webchat", {})["port"] = team["webchat_port"]
    channels.setdefault("telegram", {})["enabled"] = False
    name = f"{Path(config_name).stem}.team.json"
    (Path(state_dir) / name).write_text(json.dumps(derived, indent=2, ensure_ascii=False),
                                        encoding="utf-8")
    return name


def build_team_gateway(config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR):
    """The team as a SelmaKit gateway, wired like the agent: base set, gate, commands."""
    from selmakit import Gateway

    from chester.runtime.commands import register_runtime_commands
    from chester.runtime.wiring import register_validation_gate, selmakit_capabilities

    gateway = Gateway.from_config(
        state_dir,
        effective_config(config_name, state_dir),
        capabilities=selmakit_capabilities,
        extra_capabilities=team_capabilities(f"{state_dir}/workspace"),
    )
    register_validation_gate(gateway.agent)
    register_runtime_commands(gateway.agent)
    return gateway
