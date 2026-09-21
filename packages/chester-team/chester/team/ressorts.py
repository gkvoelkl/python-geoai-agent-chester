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
(vector ~9k characters, data ~25k), and two calls to the same ressort in one run
share no thread — the orchestrator has to restate the context. The alternative is to
carry a ``message_history`` per ressort *within* one orchestrator run: cheaper and
with context, but a ressort with a memory can drag a stale assumption along that
nobody sees any more. Left as it is until the runs say otherwise — every line in
``team-runs/ressort-calls.jsonl`` names the ressort and its duration, so "the same
ressort called three times in one run" is countable.

**No skills, for anyone in the team** (21.09.2026, after trying both places in one
day). A skill is a recipe for the **whole chain** — 8 of the 9 name tools from two to
four different ressorts. The orchestrator cannot use one because it has no tools and
must hand out goals; it read `walkability` and passed `qgis_service_area` down, which
cost a run. A ressort cannot use one either, and worse: the recipe *aims* it at
another phase. Measured the same evening — the output ressort loaded four skills
(215 s), three of them recipes for phases it does not serve, then called
`geodatasets_list`, `vector_info` and `geo_python_run`, none of which it has, and hit
its 600-second limit having produced nothing.

The knowledge that a skill carried goes into the **tool text** instead, where it is
always present and names a tool that exists (`service_area`: network reach, not a
buffer). What a skill has that no tool text has — the order of the steps — is the
orchestrator's job by construction. If the runs show the team lacking *method* rather
than tool choice, the answer is a two-layer skill (goals for the orchestrator, phase
fragments for the ressorts); that is nine files of work and wants a measurement first.

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
from pydantic_ai.messages import RetryPromptPart, ToolCallPart, ToolReturnPart

from chester import ressortcut, wrapperlayer
from chester.runtime import live, mapinspect
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
#: How much of a call is shown live while a ressort works (arguments, then result).
_LIVE_ARGS_CHARS = 160
_LIVE_RESULT_CHARS = 220
#: How much of a failed tool return is kept in the log — enough to see *why* a call
#: failed, short enough that a log of 25 calls stays readable.
_LOG_ERROR_CHARS = 200
#: Extensions whose paths in a tool return count as produced files.
_OUTPUT_EXTS = {".gpkg", ".geojson", ".shp", ".tif", ".tiff", ".csv", ".json",
                ".html", ".png", ".laz", ".copc.laz", ".city.json", ".glb"}

#: What each ressort is for — the first lines of its instructions.
_ROLE = {
    "data": "You are the DATA ressort. Get the data the task needs: find out what "
            "exists and where — catalogues, services, the local cache, official "
            "boundaries, place names — and bring it in. Official sources before OSM, "
            "the named area rather than a bounding box, and convert the download into "
            "a usable layer. If there is nothing to fetch from, the task is not yours: "
            "making a layer out of coordinates or a table is the VECTOR ressort's work.",
    "vector": "You are the VECTOR ressort. Make and compute vector layers: create one "
              "from coordinates or a table, reproject, clip, buffer, overlay, join, "
              "aggregate. Run the whole chain the task needs yourself; do not stop "
              "halfway.",
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

**Work only on the part that is yours.** If the task asks for a phase that is not
yours — fetching data when you compute, computing when you fetch — do that part not
at all: hand back what you can, and say which ressort the rest belongs to.

**If none of your tools fits the task, stop — do not try variants.** Hand back what
you have, say in `report` what is missing, and name in `open_points` the ressort that
can do it. Twenty-two attempts with the wrong tool cost the team ten minutes and
produced nothing (measured 2026-09-20).

## Asking for something back

If your task needs something you cannot produce — layers that are not in one CRS, a
boundary you were not given, data that is not on disk — put it in `needs` and hand
back. The orchestrator arranges it and calls you again with the new paths.

Say a **condition**, not a recipe: *"all inputs in one metric CRS"*, not *"run
vector_reproject"*. You do not know which tools the other ressorts have, and a step
you invent costs the team a whole run. One need is one line.

`needs` is not `open_points`: a need **blocks** this task, an open point is a doubt
about work you did. If the need blocks everything, hand back at once with empty
`outputs` — that is a correct answer, not a failure. Do the part you can first if
there is one.
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
    needs: list[str] = Field(
        default_factory=list,
        description="Conditions that must hold before this task can succeed and that "
                    "you cannot bring about yourself — each as a state, not as a tool "
                    "or a step: 'all inputs in one metric CRS', 'the official district "
                    "boundary of Regensburg as a layer'. The orchestrator arranges it "
                    "and calls you again. Leave empty when nothing is missing.",
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
    share one, which is why the merged `data` ressort costs barely more text than
    either half did."""
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
) -> Agent[None, Any]:
    """A pydantic-ai agent for one ressort. ``model`` overrides the config (tests)."""
    if name not in ressortcut.RESSORTS:
        raise ValueError(f"unknown ressort {name!r}; known: {tuple(ressortcut.RESSORTS)}")
    return Agent[None, Any](
        model if model is not None
        else _build_model(_model_name(config_name, state_dir), config_name, state_dir),
        # Report **or** plain prose. Measured 2026-09-20: the scout did its work, then
        # answered the handover in prose three times ("I have listed the files…") and
        # the run died on the output schema — work done, result lost. The paths come
        # from the tool returns anyway, so prose costs nothing but `open_points`. Same
        # principle as everywhere here: the return channel carries the load, not the
        # model's discipline.
        output_type=[RessortReport, str],
        instructions=ressort_instructions(name),
        tools=ressort_tools(name, workspace, geodata,
                            config_name=config_name, state_dir=state_dir),
        # No skills — see the module docstring. A recipe for the whole chain has no
        # reader here, and it aims a ressort at somebody else's phase.
        name=f"ressort-{name}",
        # Three tries for the structured handover, not one: a local model gets the
        # schema wrong now and then, and pydantic-ai feeds the error back so it can
        # self-correct — the judge needed the same (`testprompt.build_judge`).
        retries=3,
    )


def _record(node: Any, calls: list[str], produced: list[str], workspace: str,  # noqa: PLR0913
            outcomes: list[dict] | None = None, name: str = "") -> None:
    """One node of the run: which tool was called, how it ended, which files came back.

    Recorded as the run goes, not at the end — a cap or a failure must not take the
    record with it. Read by ``getattr`` rather than by node type: an unknown node kind
    is simply nothing to record. The price is that a renamed field would make this go
    quiet, which is why a test asserts that a scripted run records its call.

    ``outcomes`` carries the *why* into the log: after a ressort called the same wrong
    tool 22 times (2026-09-20), the names alone did not say what came back. It stays
    out of the return value — that one has to remain small enough not to be offloaded
    by `ToolOutputLimits`, or the gate would lose the paths with it.
    """
    for part in getattr(getattr(node, "model_response", None), "parts", []):
        # The handover itself is an output tool (`final_result`) — not a tool choice,
        # and counting it would skew the hit rate.
        if isinstance(part, ToolCallPart) and not part.tool_name.startswith(_OUTPUT_TOOL_PREFIX):
            calls.append(part.tool_name)
            # Live, not only in the summary at the end: a ressort call takes minutes,
            # and until it returns the watcher sees nothing at all (2026-09-20).
            live.emit(f"\n   [{name}] → {part.tool_name}"
                      f"({live.short(part.args, _LIVE_ARGS_CHARS)})")
    for part in getattr(getattr(node, "request", None), "parts", []):
        if isinstance(part, ToolReturnPart):
            produced.extend(p for p in _produced(part.content, workspace) if p not in produced)
        if isinstance(part, (ToolReturnPart, RetryPromptPart)):
            outcome = _outcome(part)
            if outcomes is not None:
                outcomes.append(outcome)
            mark = "✗" if outcome["ok"] is False else "←"
            body = outcome["error"] or live.short(getattr(part, "content", ""),
                                                  _LIVE_RESULT_CHARS)
            live.emit(f"\n   [{name}] {mark} {outcome['tool']}: {body}")


def _outcome(part: Any) -> dict:
    """What one tool call came back with: the tool, whether it worked, and why not."""
    if isinstance(part, RetryPromptPart):  # the framework rejected the call itself
        return {"tool": part.tool_name or "?", "ok": False,
                "error": str(part.content)[:_LOG_ERROR_CHARS]}
    content = part.content
    ok = content.get("ok") if isinstance(content, dict) else None
    error = ""
    if isinstance(content, dict) and ok is False:
        # Not every refusal carries an `error`: `check_crs` answers `ok: false` with its
        # findings and nothing else, and the log then read as an empty failure
        # (2026-09-21). Fall back to the return itself, short.
        error = str(content.get("error") or content.get("warning") or
                    live.short(content, _LOG_ERROR_CHARS))[:_LOG_ERROR_CHARS]
    return {"tool": part.tool_name, "ok": ok, "error": error}


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
    workspace: str = WORKSPACE_DIR, agent: Agent[None, Any] | None = None,
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
    outcomes: list[dict] = []
    started = time.monotonic()

    async def drive() -> Any:
        async with agent.iter(_prompt(task, input_paths or []),
                              usage_limits=UsageLimits(request_limit=request_limit)) as run:
            async for node in run:
                _record(node, calls, produced, workspace, outcomes, name)
            if run.result is None:  # not an assert: `python -O` would drop it
                raise RuntimeError(f"ressort {name}: the run ended without a result")
            return run.result.output

    cap = None
    error = None
    report = RessortReport(report="")
    try:
        handover = await asyncio.wait_for(drive(), timeout=timeout_s)
        report = handover if isinstance(handover, RessortReport) else RessortReport(
            report=str(handover))
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
        "needs": report.needs,
        "capped": cap is not None,
        "cap": cap,
        "error": error,
        "tools_called": calls,
        "duration_s": round(time.monotonic() - started, 1),
    }
    result["log"] = _write_log(workspace, task, result, outcomes)
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


def _write_log(workspace: str, task: str, result: dict,
               outcomes: list[dict] | None = None) -> str | None:
    """One JSON line per ressort call; best effort — a log never costs a result.

    ``calls`` holds each tool call with its outcome — in the file, not in the return:
    this is where one reads *why* a tool did not work.
    """
    try:
        log_dir = Path(workspace) / "team-runs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "ressort-calls.jsonl"
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "task": task, **{k: v for k, v in result.items() if k != "log"},
                "calls": outcomes or []}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        return str(path)
    except OSError:
        return None
