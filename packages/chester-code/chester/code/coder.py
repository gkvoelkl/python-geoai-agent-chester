"""The coder of chester-code — an agent whose only way to do geo work is a program.

Its tool surface is CodeMode's single `run_code` — strictly nothing else. Inside it, as
functions: **every** geo tool of the wrapper layer, `geo_python_run` (geopandas for
what no tool does), `inspect_map`, and SelmaKit's own tools (memory, web search and
fetch). Around it: the shared
base (run log, usage ledger, prompt cache, model limits, web instruction) **without**
planning — the program is the plan — the enforcing gate, the data commands.

Two mechanisms carry the "one program per task" idea, because instructions alone did
not (the CodeMode trial in chester-agent, 2026-10-24: gemma4 and qwen3.6-coder wrote
one tool call per `run_code` and walked the task step by step):

* :data:`_INSTRUCTIONS` — the frame comes first and shows a whole task as one program.
* :class:`~chester.code.guards.OneProgramGuardCapability` — a `run_code` that ran a
  single tool gets a correction appended to its result, where the model cannot skip
  it (the PlanGuard device). Once per run, since a last, single `render_map` program is legitimate.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset
from pydantic_ai_harness.code_mode import CodeMode

from chester import toolreturns, wrapperlayer
from chester.code.guards import ONE_PROGRAM_NOTE as ONE_PROGRAM_NOTE  # re-export
from chester.code.guards import OneProgramGuardCapability, ResultSizeGuardCapability
from chester.code.programcheck import ProgramCheckCapability
from chester.runtime.codemode import wrapper_tool_names
from chester.runtime.config import (
    CONFIG_NAME,
    STATE_DIR,
    WORKSPACE_DIR,
    config_base_url,
    config_main_model,
    config_vision_model,
    load_geodata,
)
from chester.runtime.geopython import build_geo_python_run
from chester.runtime.geotoolset import wrapper_options
from chester.runtime.mapinspect import build_inspect_map
from chester.runtime.wiring import base_capabilities

_RUN_CODE = "run_code"

#: Failed `run_code` calls in a row before the run is aborted (CodeMode's default is 3).
#: Measured 2026-10-24 (`mean-elevation-per-district`, qwen3.6-coder): a stray import, a
#: mistyped WFS URL and one more try — and the run died with no output, although each
#: error was being fixed. For an agent whose only tool is a program, a failed program is
#: a normal correction round, not a malfunction; the context still bounds the total.
RUN_CODE_RETRIES = 8

_INSTRUCTIONS = """\
## You are Chester Code: you solve every geo task with one program

You have no geo tools of your own. Every geo tool — geocoding, OpenStreetMap, official
boundaries, elevation models, aerial imagery, statistics, the `vector_*` operations,
raster and terrain analysis, maps — is an **async function inside `run_code`**. To do a
task, **write one Python program that does all of it, and run it: call the `run_code`
tool with the program as its `code` argument.** Never write the program into your
answer — a program in the answer has not run, and none of its files exist. Call only
the functions listed in the `run_code` description; do not invent any.

1. Think the task through first: which data, which area, which CRS, which computation,
   which output. A named area is clipped to its official boundary, never left as a
   bounding box — and **the result too**: an operation that reaches past the boundary
   (a buffer, a service area, a Voronoi cell) is clipped again at the end with
   `fetch_boundaries` + `vector_clip`, so nothing the answer delivers lies outside the
   named area. Distances and areas are computed in a metric CRS (EPSG:25832 in
   Germany).
2. Write the whole chain as one program. Each tool takes and returns **paths**; pass
   one result's `"output"` to the next call. Read the tools' docstrings in the
   `run_code` description for their exact parameters and return keys. In a result,
   `"output"` is the written file's **path** and `"features"` is a **count** (an int,
   never a list — `len(r["features"])` fails).
   Write the steps **at top level** — top-level `await` works. Do not wrap them in
   `async def main()`: a function that is defined but never called runs nothing.
3. Check every step with `assert r.get("ok"), r` — a failed step stops the program with
   the tool's own error, and you fix that step and send the **whole corrected program**
   again.
4. End the program with one dict: the output paths, the numbers the answer needs and
   any `warning` a tool returned. Return paths and numbers, not whole layers.

Example — "500 m buffers around all schools in Regensburg, on a map":

```python
schools = await osm_features(place="Regensburg", tags={"amenity": "school"},
                             output_path="schools.gpkg")
assert schools.get("ok"), schools
parts = await vector_split_by_geometry(path=schools["output"], output_prefix="schools")
assert parts.get("ok"), parts
layers = [p["output"] for p in parts["parts"]
          if p["geometry_type"] in ("Point", "Polygon", "MultiPolygon")]
merged = await vector_merge(input_paths=layers, output_path="schools_all.gpkg")
assert merged.get("ok"), merged
metric = await vector_reproject(input_path=merged["output"], target_crs="EPSG:25832",
                                output_path="schools_25832.gpkg")
assert metric.get("ok"), metric
buffers = await vector_buffer(input_path=metric["output"], distance=500,
                              output_path="schools_buffer_500m.gpkg")
assert buffers.get("ok"), buffers
city = await fetch_boundaries(output_path="regensburg_boundary.gpkg", match="Regensburg")
assert city.get("ok"), city
clipped = await vector_clip(input_path=buffers["output"], overlay_path=city["output"],
                            output_path="schools_buffer_500m_city.gpkg")
assert clipped.get("ok"), clipped
shown = await render_map(layers=[clipped["output"]], output_path="schools_buffer_map.html",
                         title="Schools in Regensburg, 500 m buffers")
{"buffers": clipped["output"], "map": shown["output"], "schools": schools["features"],
 "warning": schools.get("warning")}
```

The sandbox runs a Python subset: standard library only, no third-party imports, no
direct file access. The tool functions are **already defined** — call them directly;
there is no module to import them from (`from tools import *` fails).

For a computation no tool expresses (a Gini coefficient, a custom statistic), call
`await geo_python_run(code="...")` **inside** your program: it runs a geopandas snippet
and returns its `result`. To look at the finished map, call
`await inspect_map(...)` **once**, as the last step of the work program — not again in
a program of its own.

In your answer, link the map the program returned and state the numbers it returned —
nothing the program did not produce.
"""

#: The retry for an answer that shows a program instead of running it.
UNRUN_PROGRAM_NOTE = (
    "Your answer contains a program that calls Chester tools, but you never ran it — "
    "no `run_code` call happened, so none of its files exist. Call the `run_code` tool "
    "with this program as its `code` argument now, then answer from what it returned."
)
_AWAITED_CALL = re.compile(r"\bawait\s+([a-z_][a-z0-9_]*)\s*\(")


def make_unrun_program_check(tool_names: frozenset[str] | None = None):
    """An ``output_validator``: retry an answer that writes a tool program but never ran one.

    Measured 2026-10-24, the first chester-code run (`buffer-schools-500m`): gemma4 wrote
    exactly the program the frame asks for — `osm_features` → `vector_reproject` →
    `vector_buffer` → `render_map`, with asserts — as a Markdown block **in its answer**.
    Nothing ran; the gate could only note that the four files it named were missing.
    """
    from pydantic_ai import ModelRetry
    from selmakit import tool_returns

    async def unrun_program(ctx, output):
        if not isinstance(output, str):
            return output
        names = tool_names if tool_names is not None else wrapper_tool_names()
        if not any(n in names for n in _AWAITED_CALL.findall(output)):
            return output
        if any(name == _RUN_CODE for name, _content in tool_returns(ctx)):
            return output
        raise ModelRetry(UNRUN_PROGRAM_NOTE)

    return unrun_program


def mcp_only_modules() -> frozenset[str]:
    """The wrapper modules only chester-mcp ships (`gatetools`, `artifacttools`) — derived.

    They are the MCP adapter's stand-ins for what an agent on chester-runtime has built
    in: `validate_result` is the gate as a fact-finder for foreign clients. In the coder
    it duplicated the gate and misled the run — measured 2026-10-24, a program passed the
    map's PNG to it, got "unreadable, must_fix" and spent the rest of the run "fixing" a
    correct result. Read off where the modules live, so a new MCP hull stays out too.
    """
    import chester

    return frozenset(p.stem for d in chester.__path__ if Path(d).parent.name == "chester-mcp"
                     for p in Path(d).glob("*tools.py"))


def code_tools(workspace: str = WORKSPACE_DIR, *, config_name: str = CONFIG_NAME,
               state_dir: str = STATE_DIR) -> list:
    """Every tool the coder's programs can call: the wrapper layer and two agent-level ones.

    The wrapper tools get their result types (:mod:`chester.toolreturns`), so the stub in
    the `run_code` description shows e.g. ``features: int`` instead of ``-> dict``.
    """
    geodata = load_geodata(config_name, state_dir)
    return [
        *map(toolreturns.typed, wrapperlayer.collect_tools(
            workspace, options=wrapper_options(geodata), exclude=mcp_only_modules())),
        build_geo_python_run(workspace),
        build_inspect_map(workspace, vision_model=config_vision_model(config_name, state_dir),
                          base_url=config_base_url(config_name, state_dir),
                          main_model=config_main_model(config_name, state_dir)),
    ]


@dataclass
class CoderCapability(AbstractCapability[Any]):
    """The geo tools as one toolset (CodeMode puts them into `run_code`) and the frame.

    With ``planning``, each run starts with the requirement step (:mod:`chester.code.planner`):
    :meth:`for_run` returns a per-run copy carrying the plan, whose instructions add the
    requirement and the chosen sources' handbooks and whose toolset drops the source
    functions of the sources not chosen.
    """

    workspace: str = WORKSPACE_DIR
    config_name: str = CONFIG_NAME
    state_dir: str = STATE_DIR
    planning: bool = True
    requirement: Any = None

    async def for_run(self, ctx):
        from dataclasses import replace

        from chester.code import planner

        text = planner.prompt_text(ctx.prompt)
        if not self.planning or not text.strip():
            return self
        req = await planner.plan(ctx.model, text)
        return replace(self, requirement=req) if req is not None else self

    def get_instructions(self):
        from chester.code import handbooks

        if self.requirement is None:
            return f"{_INSTRUCTIONS}\n{handbooks.ANALYSIS_NOTES}"
        from chester.code import planner

        return (f"{_INSTRUCTIONS}\n{handbooks.ANALYSIS_NOTES}\n"
                f"{planner.instructions(self.requirement)}")

    def get_toolset(self) -> AgentToolset[Any] | None:
        tools = [*code_tools(self.workspace, config_name=self.config_name,
                             state_dir=self.state_dir), *web_tools()]
        chosen = self.requirement.sources() if self.requirement is not None else []
        if chosen:
            from chester.code import handbooks

            keep = handbooks.allowed_functions(chosen, {_name(t) for t in tools})
            tools = [t for t in tools if _name(t) in keep]
        return FunctionToolset(tools=tools)


def _name(tool) -> str:
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))


def web_tools() -> list:
    """SelmaKit's local web search and fetch, as plain tools so CodeMode sandboxes them.

    In SelmaKit's `WebSearch`/`WebFetch` capabilities they are the *local fallback* of a
    provider-native tool and carry `unless_native`; CodeMode leaves such tools outside
    `run_code`, so the model saw `duckduckgo_search` and `web_fetch` as direct tools
    beside it (measured 2026-10-24 with the real model's profile). Here they are ordinary
    tools — the same functions, the same loopback-tolerant, exception-guarded fetch — and
    the two capabilities are dropped in :func:`build_code_gateway`.
    """
    from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool
    from selmakit.gateway import local_web_fetch

    return [duckduckgo_search_tool(), local_web_fetch().local]


def code_capabilities(workspace_dir: str = WORKSPACE_DIR, *, config_name: str = CONFIG_NAME,
                      state_dir: str = STATE_DIR, planning: bool = True) -> list:
    """The coder's set: the shared base without planning, the geo tools, CodeMode, the guard.

    Planning goes because the program is the plan — a plan of four steps walked one
    `run_code` at a time is exactly what this architecture exists to avoid. Tool-output
    offloading goes because its hook also fires for nested calls: a large `vector_info`
    would reach the program as a text preview instead of a dict; the result-size guard
    bounds the program's own result instead.
    """
    from pydantic_ai_harness.planning import Planning
    from pydantic_ai_harness.tool_output_limits import ToolOutputLimits

    from chester.runtime.planguard import PlanGuardCapability

    dropped = (Planning, PlanGuardCapability, ToolOutputLimits)
    base = [c for c in base_capabilities(workspace_dir, config_name=config_name,
                                         state_dir=state_dir, skills=False)
            if not isinstance(c, dropped)]
    return [
        # First, so the frame "one program per task" opens the instructions.
        CoderCapability(workspace=workspace_dir, config_name=config_name, state_dir=state_dir,
                        planning=planning),
        *base,
        # `"all"`: every tool of the agent goes into `run_code` — the geo tools *and*
        # SelmaKit's own (memory, web). The model sees exactly one tool (2026-10-24).
        CodeMode(tools="all", max_retries=RUN_CODE_RETRIES),
        OneProgramGuardCapability(),
        ResultSizeGuardCapability(),
        # Before execution: a program that measures degrees or drops the polygon parts
        # does not run (`programcheck`, Phase SP). `CHESTER_PROGRAMCHECK=0` for A/B runs.
        *([ProgramCheckCapability()] if os.environ.get("CHESTER_PROGRAMCHECK") != "0" else []),
    ]


#: Ports of the coder when the ``code`` block names none — clear of the agent (8000/8501)
#: and the team (8100/8601/8602), so all three can run side by side.
DEFAULT_WEBCHAT_PORT = 8200
DEFAULT_DASHBOARD_PORT = 8701
DEFAULT_BENCH_PORT = 8702


def code_block(config: dict | None = None) -> dict:
    """The ``code`` block with its defaults filled in — of ``config`` if given, else of
    the main config file. Same shape as chester-team's ``team`` block."""
    from chester.runtime.config import config_block

    raw = (config or {}).get("code") if config is not None else config_block("code")
    block = raw if isinstance(raw, dict) else {}
    return {
        "webchat_port": int(block.get("webchat_port") or DEFAULT_WEBCHAT_PORT),
        "dashboard_port": int(block.get("dashboard_port") or DEFAULT_DASHBOARD_PORT),
        "bench_port": int(block.get("bench_port") or DEFAULT_BENCH_PORT),
    }


def effective_config(config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR) -> str:
    """Write the coder's effective config next to the main one; return its file name.

    The main config with the coder's overrides — its own web-chat port, Telegram off
    (two bots cannot share one token) — as ``.chester/<name>.code.json``, rewritten on
    every start. SelmaKit reads config only through ``load_config``; this keeps one
    source of truth without reaching into its internals (the team does the same).
    """
    import copy
    import json

    source = Path(state_dir) / config_name
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    derived = copy.deepcopy(data)
    channels = derived.setdefault("channels", {})
    channels.setdefault("webchat", {})["port"] = code_block(data)["webchat_port"]
    channels.setdefault("telegram", {})["enabled"] = False
    name = f"{Path(config_name).stem}.code.json"
    (Path(state_dir) / name).write_text(json.dumps(derived, indent=2, ensure_ascii=False),
                                        encoding="utf-8")
    return name


def build_code_gateway(config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR):
    """The coder as a SelmaKit gateway, wired like the agent: its set, gate, commands."""
    from selmakit import Gateway

    from chester.runtime.commands import register_runtime_commands
    from chester.runtime.wiring import capability_filter, register_validation_gate

    workspace = f"{state_dir}/workspace"
    gateway = Gateway.from_config(
        state_dir,
        effective_config(config_name, state_dir),
        # No skills: a skill is a recipe of single tool calls, the opposite of one program.
        # Web search/fetch come back as plain tools inside `run_code` (`web_tools`).
        capabilities=capability_filter(frozenset({"Skills", "WebSearch", "WebFetch"})),
        extra_capabilities=code_capabilities(workspace, config_name=config_name,
                                             state_dir=state_dir),
    )
    # Before the gate: an unrun program is the one defect the gate can only half see.
    gateway.agent.output_validator(make_unrun_program_check())
    register_validation_gate(gateway.agent, workspace, state_dir)
    register_runtime_commands(gateway.agent, workspace)
    return gateway
