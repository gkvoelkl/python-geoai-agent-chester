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

**A ressort is built per call, and is stateless between calls.** No ressort agent
exists until the orchestrator calls one, and each call builds a fresh one — measured
2026-09-20: 7 ms on the first call (module imports), under 1 ms after, against
minutes for a model call, so there is nothing to cache and no state to keep. What a
ressort knows is what the task says and what its input paths hold; the state lives on
disk, which is the blackboard idea.

The price, not yet measured: every call pays the full prefill of its instructions
(vector ~9k characters, scout ~25k), and two calls to the same ressort in one run
share no thread — the orchestrator has to restate the context. The alternative is to
carry a ``message_history`` per ressort *within* one orchestrator run: cheaper and
with context, but a ressort with a memory can drag a stale assumption along that
nobody sees any more. Left as it is until the runs say otherwise — every line in
``team-runs/ressort-calls.jsonl`` names the ressort and its duration, so "the same
ressort called three times in one run" is countable.

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
    CONFIG_NAME,
    STATE_DIR,
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


def role(ressort: str) -> str:
    """What a ressort is for, in one sentence — the first lines of its instructions and
    the first lines of the tool description the orchestrator reads."""
    return _ROLE[ressort]


def _wrapper_options(geodata: dict) -> dict[str, dict[str, Any]]:
    """Config for the wrapper modules that take any — as `agent_build` threads it."""
    from chester.geocache import DEFAULT_TTL_DAYS

    return {
        "connectorstools": {"roots": geodata.get("roots"), "postgis": geodata.get("postgis")},
        # `ttl_days` too, not only `ttl_by_source`: without it a ressort's
        # `geocache_list` prunes on the 30-day default while the config says otherwise
        # — it would delete the user's cached data (found in review, 2026-09-20).
        "inventorytools": {"roots": geodata.get("roots"),
                           "default_ttl_days": geodata.get("ttl_days") or DEFAULT_TTL_DAYS,
                           "ttl_by_source": geodata.get("ttl_by_source")},
        "stactools": {"extra_catalogs": geodata.get("stac_catalogs")},
    }


def ressort_tools(name: str, workspace: str, geodata: dict | None = None, *,
                  config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR) -> list[Callable]:
    """The tools of one ressort: its slice, the checks, and its agent-level tools."""
    wanted = set(ressortcut.tools_for(name))
    geodata = load_geodata() if geodata is None else geodata
    tools = wrapperlayer.collect_tools(
        workspace, options=_wrapper_options(geodata), only=wanted
    )
    if name in AGENT_LEVEL["geo_python_run"]:
        tools.append(build_geo_python_run(workspace))
    if name in AGENT_LEVEL["inspect_map"]:
        tools.append(build_inspect_map(
            workspace, vision_model=config_vision_model(), base_url=config_base_url(),
            main_model=_model_name(config_name, state_dir),
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
    """The wrapper modules that supply at least one tool of the ressort (checks too).

    Read off the collected tools' ``__module__`` — the cut says ressort → tools, and
    the reverse (tool → module, and thus which instruction text applies) is already in
    the functions themselves. A second list would only drift.
    """
    wanted = set(ressortcut.tools_for(name))
    owners = {t.__module__.rsplit(".", 1)[-1]
              for t in wrapperlayer.collect_tools("/tmp/chester-ressort-probe", only=wanted)}
    if name in AGENT_LEVEL["geo_python_run"]:
        owners.add("vectortools")  # the vector text is where geo_python_run is explained
    return owners


def _model_name(config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR) -> str:
    """The ressorts' model: ``team.ressort_model``, else the run's own main model."""
    block = config_block("team", config_name, state_dir)
    return str(block.get("ressort_model") or config_main_model())


def _build_model(model_name: str, config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR):
    """The configured model with that config's endpoint and timeout."""
    from selmakit.config import build_model, load_config

    cfg = load_config(state_dir, config_name).model.model_copy(update={"model": model_name})
    return build_model(cfg)


def build_ressort_agent(  # noqa: PLR0913  # one agent: which, where, model, data, config
    name: str, workspace: str = WORKSPACE_DIR, *, model: Any = None,
    geodata: dict | None = None, config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR,
) -> Agent[None, RessortReport]:
    """A pydantic-ai agent for one ressort. ``model`` overrides the config (tests)."""
    if name not in ressortcut.RESSORTS:
        raise ValueError(f"unknown ressort {name!r}; known: {tuple(ressortcut.RESSORTS)}")
    return Agent[None, RessortReport](
        model if model is not None
        else _build_model(_model_name(config_name, state_dir), config_name, state_dir),
        output_type=RessortReport,
        instructions=ressort_instructions(name),
        tools=ressort_tools(name, workspace, geodata,
                            config_name=config_name, state_dir=state_dir),
        name=f"ressort-{name}",
        # Three tries for the structured handover, not one: a local model gets the
        # schema wrong now and then, and pydantic-ai feeds the error back so it can
        # self-correct — the judge needed the same (`testprompt.build_judge`).
        retries=3,
    )


def _record(node: Any, calls: list[str], produced: list[str], workspace: str) -> None:
    """One node of the run: which tool was called, which files came back.

    Recorded as the run goes, not at the end — a cap or a failure must not take the
    record with it. Read by ``getattr`` rather than by node type: an unknown node kind
    is simply nothing to record. The price is that a renamed field would make this go
    quiet, which is why a test asserts that a scripted run records its call.
    """
    for part in getattr(getattr(node, "model_response", None), "parts", []):
        # The handover itself is an output tool (`final_result`) — not a tool choice,
        # and counting it would skew the hit rate.
        if isinstance(part, ToolCallPart) and not part.tool_name.startswith(_OUTPUT_TOOL_PREFIX):
            calls.append(part.tool_name)
    for part in getattr(getattr(node, "request", None), "parts", []):
        if isinstance(part, ToolReturnPart):
            produced.extend(p for p in _produced(part.content, workspace) if p not in produced)


def _produced(content: Any, workspace: str) -> list[str]:
    """Existing files named in a tool return (walks nested dicts and lists).

    Reads only: a URL is not a path (a STAC or catalogue return is full of
    ``https://host/x.csv``), and nothing here may create a directory — `resolve_path`
    does that for a write, which would litter the cache with folders named after
    hosts (found in review, 2026-09-20). So the cache path is built directly and the
    file must already exist.
    """
    cache = Path(workspace) / "geocache"
    found: list[str] = []
    stack = [content]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, str) and len(item) < 512 and "://" not in item and \
                any(item.lower().endswith(ext) for ext in _OUTPUT_EXTS):
            candidate = item if os.path.isabs(item) else str(cache / Path(item).name)
            if os.path.isfile(candidate) and candidate not in found:
                found.append(candidate)
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
    config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR,
) -> dict:
    """Run one ressort on one task and return its handover (never raises on a cap)."""
    team = config_block("team", config_name, state_dir)
    request_limit = request_limit or int(team.get("ressort_request_limit") or
                                         DEFAULT_REQUEST_LIMIT)
    timeout_s = timeout_s or float(team.get("ressort_timeout_s") or DEFAULT_TIMEOUT_S)
    agent = agent or build_ressort_agent(name, workspace, config_name=config_name,
                                         state_dir=state_dir)
    calls: list[str] = []
    produced: list[str] = []
    started = time.monotonic()

    async def drive() -> RessortReport:
        async with agent.iter(_prompt(task, input_paths or []),
                              usage_limits=UsageLimits(request_limit=request_limit)) as run:
            async for node in run:
                _record(node, calls, produced, workspace)
            if run.result is None:  # not an assert: `python -O` would drop it
                raise RuntimeError(f"ressort {name}: the run ended without a result")
            return run.result.output

    cap = None
    error = None
    report = RessortReport(report="")
    try:
        report = await asyncio.wait_for(drive(), timeout=timeout_s)
    except TimeoutError:
        cap = f"time limit of {timeout_s:.0f}s"
    except UsageLimitExceeded as exc:
        cap = f"request limit of {request_limit} ({exc})"
    except Exception as exc:  # noqa: BLE001 - a failing ressort must never take the team down
        # First team run, 2026-09-19: the scout could not produce its structured
        # handover, pydantic-ai raised, and the exception went up through the ressort
        # tool and ended the orchestrator's whole run. A ressort reports failure; the
        # orchestrator decides what to do about it.
        error = f"{type(exc).__name__}: {exc}"
    # One spelling per file, and absolute, as the contract promises: the model tends to
    # list a bare name while the tool return holds the resolved path (first real run,
    # 2026-09-19 — the same file came back twice).
    resolved = (str(Path(resolve_path(p, workspace)).resolve())
                for p in [*report.outputs, *produced])
    outputs = [p for p in dict.fromkeys(resolved) if os.path.isfile(p)]
    result = {
        "ok": cap is None and error is None,
        "ressort": name,
        "outputs": outputs,
        "report": _summary(report.report, cap, error),
        "open_points": report.open_points,
        "capped": cap is not None,
        "cap": cap,
        "error": error,
        "tools_called": calls,
        "duration_s": round(time.monotonic() - started, 1),
    }
    result["log"] = _write_log(workspace, task, result)
    return result


def _summary(report: str, cap: str | None, error: str | None) -> str:
    """What the orchestrator reads first: the ressort's report, or why there is none."""
    if error:
        return (f"Failed: {error}. The files produced so far are in `outputs`; hand the "
                "task again, narrower, or to another ressort.")
    if cap:
        return (f"Stopped at the {cap} — the files produced so far are in `outputs`, the "
                "rest of the task is not done.")
    return report


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
