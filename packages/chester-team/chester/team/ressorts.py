"""The ressort agents of chester-team — one agent per phase of the chain.

The orchestrator (T3) calls each ressort as a tool: a task and the input paths go
in, the produced paths and a short report come out. A ressort is **not** a SelmaKit
agent — no sessions, no channels, no persona, no slash commands. It is a plain
pydantic-ai agent over its slice of chester-geo-tools (`chester.ressortcut`), plus
the check tools every ressort gets, plus two agent-level tools from chester-runtime:

* ``geo_python_run`` for *vector* and *raster* — without QGIS, turning a table into
  points or computing a Gini coefficient goes only through a snippet (baseline of
  2026-09-19: four of ten probes used it);
* ``inspect_map`` for all — it is a visual **check**, and checking is no ressort.

Three rules carry the design (`internal/TODO.md`, KP.5 T2):

1. **Paths are the handover.** The return names every file the ressort produced;
   the orchestrator's gate sees a ressort's tool return, never the tool calls inside
   it, so a path that is not in the return is invisible to validation. The paths are
   taken from the tool returns as well as from the model's own report — a model that
   forgets to list a file does not hide it.
2. **A cap says that it capped.** A request limit and a time limit bound every call,
   and hitting either returns ``capped: true`` with the work done so far — a silent
   stop inside a ressort would be worse than in the single agent, because nobody
   watches it.
3. **Every call leaves a record** (``team-runs/`` in the workspace), because the
   concept's figure "tool hit rate per ressort" needs the calls inside a ressort, and
   the session log sees only the orchestrator.

The model comes from the config only (``team.ressort_model``, default: the main
model) — the LLM layer stays config-only.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ToolCallPart, ToolReturnPart

from chester import ressortcut, wrapperlayer
from chester.runtime import mapinspect
from chester.runtime.config import (
    WORKSPACE_DIR,
    config_base_url,
    config_block,
    config_main_model,
    config_vision_model,
    load_geodata,
)
from chester.runtime.geopython import build_geo_python_run
from chester.runtime.mapinspect import build_inspect_map
from chester.workspace import resolve_path

#: Agent-level tools outside the wrapper layer, and the ressorts that get them.
AGENT_LEVEL: dict[str, tuple[str, ...]] = {
    "geo_python_run": ("vector", "raster"),
    "inspect_map": tuple(ressortcut.RESSORTS),
}
DEFAULT_REQUEST_LIMIT = 25
#: pydantic-ai names its structured-output tool `final_result` (with a suffix when
#: there are several output types).
_OUTPUT_TOOL_PREFIX = "final_result"
DEFAULT_TIMEOUT_S = 600.0
#: Extensions whose paths in a tool return count as produced files.
_OUTPUT_EXTS = {".gpkg", ".geojson", ".shp", ".tif", ".tiff", ".csv", ".json",
                ".html", ".png", ".laz", ".copc.laz", ".city.json", ".glb"}

#: What each ressort is for — the first lines of its instructions.
_ROLE = {
    "scout": "You are the SCOUT. Find out what data exists for the task and where: "
             "catalogues, services, the local cache, official boundaries, place names. "
             "Report sources and what they can and cannot do; fetch only for a first look.",
    "acquisition": "You are the ACQUISITION ressort. Bring the data the task needs into "
                   "the cache — official sources before OSM, the named area rather than "
                   "a bounding box — and convert it into a layer the next step can use.",
    "vector": "You are the VECTOR ressort. Compute on vector layers: reproject, clip, "
              "buffer, overlay, join, aggregate. Run the whole chain the task needs "
              "yourself; do not stop halfway.",
    "raster": "You are the RASTER/TERRAIN ressort. Compute on rasters and elevation "
              "models: indices, terrain derivatives, zonal statistics, sampling.",
    "output": "You are the OUTPUT ressort. Present results: maps, 3D views, and a look "
              "at what was made. Do not recompute anything.",
}
_CONTRACT = """
## Handing back

You are one ressort of a team; an orchestrator gave you this task and will pass your
result on. Work only on this task. Put the **absolute path** of every file the next
step needs into `outputs` — a file you do not list may be lost to the team. Say in
`report` what you did and what you found, and in `open_points` what is doubtful or
left undone. Check your result with the check tools before you hand it back.
"""


class RessortReport(BaseModel):
    """What a ressort agent hands back to the orchestrator."""

    outputs: list[str] = Field(
        default_factory=list,
        description="Absolute paths of every file you produced that the next step needs.",
    )
    report: str = Field(description="What you did and what you found, in a few sentences.")
    open_points: list[str] = Field(
        default_factory=list, description="What is doubtful or left undone."
    )


def _wrapper_options(geodata: dict) -> dict[str, dict[str, Any]]:
    """Config for the wrapper modules that take any — as `agent_build` threads it."""
    return {
        "connectorstools": {"roots": geodata.get("roots"), "postgis": geodata.get("postgis")},
        "inventorytools": {"roots": geodata.get("roots"),
                           "ttl_by_source": geodata.get("ttl_by_source")},
        "stactools": {"extra_catalogs": geodata.get("stac_catalogs")},
    }


def ressort_tools(name: str, workspace: str, geodata: dict | None = None) -> list[Callable]:
    """The tools of one ressort: its slice, the checks, and its agent-level tools."""
    wanted = set(ressortcut.tools_for(name))
    geodata = load_geodata() if geodata is None else geodata
    tools = [t for t in wrapperlayer.collect_tools(workspace, options=_wrapper_options(geodata))
             if t.__name__ in wanted]
    if name in AGENT_LEVEL["geo_python_run"]:
        tools.append(build_geo_python_run(workspace))
    if name in AGENT_LEVEL["inspect_map"]:
        tools.append(build_inspect_map(
            workspace, vision_model=config_vision_model(), base_url=config_base_url(),
            main_model=_model_name(),
        ))
    return tools


def ressort_instructions(name: str) -> str:
    """Role, the instruction text of every wrapper module the ressort draws on, and the
    handover contract. Each text once — the eight modules of the acquisition group
    share one."""
    modules = _owning_modules(name)
    texts: list[str] = []
    for mod in sorted(modules):
        text = wrapperlayer.module_instructions(importlib.import_module(f"chester.{mod}"))
        if text.strip() and text not in texts:
            texts.append(text)
    if name in AGENT_LEVEL["inspect_map"]:
        texts.append(mapinspect.INSTRUCTIONS)
    return "\n\n".join([_ROLE[name], *texts, _CONTRACT])


def _owning_modules(name: str) -> set[str]:
    """The wrapper modules that supply at least one tool of the ressort (checks too)."""
    wanted = set(ressortcut.tools_for(name))
    owners = set()
    for mod in wrapperlayer.wrapper_modules():
        module = importlib.import_module(f"chester.{mod}")
        names = {t.__name__ for t in module.build_tools("/tmp/chester-ressort-probe")}
        if names & wanted:
            owners.add(mod)
    if name in AGENT_LEVEL["geo_python_run"]:
        owners.add("vectortools")  # the vector text is where geo_python_run is explained
    return owners


def _model_name() -> str:
    return str(config_block("team").get("ressort_model") or config_main_model())


def _build_model(model_name: str):
    """The configured model with the main config's endpoint and timeout."""
    from selmakit.config import build_model, load_config

    from chester.runtime.config import CONFIG_NAME, STATE_DIR

    cfg = load_config(STATE_DIR, CONFIG_NAME).model.model_copy(update={"model": model_name})
    return build_model(cfg)


def build_ressort_agent(name: str, workspace: str = WORKSPACE_DIR, *, model: Any = None,
                        geodata: dict | None = None) -> Agent[None, RessortReport]:
    """A pydantic-ai agent for one ressort. ``model`` overrides the config (tests)."""
    if name not in ressortcut.RESSORTS:
        raise ValueError(f"unknown ressort {name!r}; known: {tuple(ressortcut.RESSORTS)}")
    return Agent[None, RessortReport](
        model if model is not None else _build_model(_model_name()),
        output_type=RessortReport,
        instructions=ressort_instructions(name),
        tools=ressort_tools(name, workspace, geodata),
        name=f"ressort-{name}",
    )


def _produced(content: Any, workspace: str) -> list[str]:
    """Existing files named in a tool return (walks nested dicts and lists)."""
    found: list[str] = []
    stack = [content]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str) and len(item) < 512 and \
                any(item.lower().endswith(ext) for ext in _OUTPUT_EXTS):
            path = resolve_path(item, workspace)
            if os.path.isfile(path) and path not in found:
                found.append(path)
    return found


def _prompt(task: str, input_paths: list[str]) -> str:
    if not input_paths:
        return task
    listed = "\n".join(f"- {p}" for p in input_paths)
    return f"{task}\n\nInput files:\n{listed}"


async def run_ressort(  # noqa: PLR0913  # one call carries task, place, model and both caps
    name: str, task: str, input_paths: list[str] | None = None, *,
    workspace: str = WORKSPACE_DIR, agent: Agent[None, RessortReport] | None = None,
    request_limit: int | None = None, timeout_s: float | None = None,
) -> dict:
    """Run one ressort on one task and return its handover (never raises on a cap)."""
    team = config_block("team")
    request_limit = request_limit or int(team.get("ressort_request_limit") or
                                         DEFAULT_REQUEST_LIMIT)
    timeout_s = timeout_s or float(team.get("ressort_timeout_s") or DEFAULT_TIMEOUT_S)
    agent = agent or build_ressort_agent(name, workspace)
    calls: list[str] = []
    produced: list[str] = []
    started = time.monotonic()

    async def drive() -> RessortReport:
        async with agent.iter(_prompt(task, input_paths or []),
                              usage_limits=UsageLimits(request_limit=request_limit)) as run:
            async for node in run:
                for part in getattr(getattr(node, "model_response", None), "parts", []):
                    # The handover itself is an output tool (`final_result`) — not
                    # a tool choice, and counting it would skew the hit rate.
                    if isinstance(part, ToolCallPart) and \
                            not part.tool_name.startswith(_OUTPUT_TOOL_PREFIX):
                        calls.append(part.tool_name)
                for part in getattr(getattr(node, "request", None), "parts", []):
                    if isinstance(part, ToolReturnPart):
                        produced.extend(p for p in _produced(part.content, workspace)
                                        if p not in produced)
            assert run.result is not None
            return run.result.output

    cap = None
    report = RessortReport(report="")
    try:
        report = await asyncio.wait_for(drive(), timeout=timeout_s)
    except TimeoutError:
        cap = f"time limit of {timeout_s:.0f}s"
    except UsageLimitExceeded as exc:
        cap = f"request limit of {request_limit} ({exc})"
    # One spelling per file, and absolute, as the contract promises: the model tends to
    # list a bare name while the tool return holds the resolved path (first real run,
    # 2026-09-19 — the same file came back twice).
    resolved = (str(Path(resolve_path(p, workspace)).resolve())
                for p in [*report.outputs, *produced])
    outputs = [p for p in dict.fromkeys(resolved) if os.path.isfile(p)]
    result = {
        "ok": cap is None,
        "ressort": name,
        "outputs": outputs,
        "report": report.report if cap is None else
        f"Stopped at the {cap} — the files produced so far are in `outputs`, the rest "
        "of the task is not done.",
        "open_points": report.open_points,
        "capped": cap is not None,
        "cap": cap,
        "tools_called": calls,
        "duration_s": round(time.monotonic() - started, 1),
    }
    result["log"] = _write_log(workspace, task, result)
    return result


def _write_log(workspace: str, task: str, result: dict) -> str | None:
    """One JSON line per ressort call; best effort — a log never costs a result."""
    try:
        log_dir = Path(workspace) / "team-runs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "ressort-calls.jsonl"
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "task": task, **{k: v for k, v in result.items() if k != "log"}}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        return str(path)
    except OSError:
        return None
