"""Chester-MCP — Chester's geo tools for a foreign client, over stdio.

Phase KM, step 3. A **channel** beside webchat and Telegram, not a fork: the server
collects the wrapper layer (`chester/*tools.py`) and registers it. There is no second
tool definition — that is the whole purpose of the layer.

**What is deliberately missing.**

* **No instruction text.** Neither preamble nor rule block. What works inside a
  foreign harness is not what a tool *says* but what it *does* and *reports back*:
  `osm_features` clips a named place to the official boundary and reports
  `clipped_to_place`; metric work in a geographic CRS is refused, not discouraged; a
  mistyped layer name gets `did_you_mean`. A warning in the return value is a fact
  about the world, not an order — it works even on a model that ignores instructions
  in tool texts (`internal/chester-mcp.md` §4b). Tool-local documentation lives in the
  **docstring**; for an MCP client it is the only text channel that demonstrably
  reaches the model (measured 2026-09-13).
* **No `geo_python_run`, no `qgis_python`.** The escape hatch stays reserved for
  Chester's own agent, which keeps the server free of remote execution. The price,
  named: whatever no tool covers is unreachable over MCP.
* **No framework machinery.** Chester's agent carries two tools that come from
  *SelmaKit* and do not belong here: `write_plan` (planning — exactly the guidance
  whose contribution F+ ↔ F+MCP measures) and `read_tool_result`. The second has a
  consequence that belongs in the measurement: Chester **truncates** long tool
  answers and hands over a handle; over MCP every answer reaches the client
  **untruncated** and costs its context. The counts and their derivation are in
  `doc/usage.md` (Chester-MCP), checked by `tests/test_doc_counts.py`.
* **No enforcement.** `validate_result` returns the same findings as the gate, but
  nothing makes a foreign client call it. That missing enforcement is exactly what
  cell F+MCP measures — it appears as `enforced: false` in every return value.

**Local only, stdio only**: one client per process, one shared workspace as today.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from chester.workspace import DEFAULT_WORKSPACE

#: Wrapper modules that are **not** served — and this is empty on purpose.
#: Decided 2026-09-13: **serve everything**, no curated subset. The four exceptions
#: (`geo_python_run`, `qgis_python`, `inspect_map`, skillguide/runlog) sit outside the
#: wrapper layer anyway — they are bound to the framework, not excluded. The set
#: stays as a named place in case one is ever added.
EXCLUDED: frozenset[str] = frozenset()


def wrapper_modules() -> list[str]:
    """Names of the wrapper modules, in catalogue order — without the excluded ones."""
    here = Path(__file__).parent
    return sorted(
        p.stem for p in here.glob("*tools.py") if p.stem not in EXCLUDED
    )


def collect_tools(workspace: str) -> list[Callable[..., dict]]:
    """Every tool of the wrapper layer, bound to ``workspace``.

    A module without `build_tools` is an **error**, not an omission: skipping it
    silently would mean serving a smaller catalogue without anyone noticing — exactly
    what happened to `vectoroptools` while it was called `op_tools` (2026-09-14).
    `tests/test_structure.py` checks the same contract.
    """
    tools: list[Callable[..., dict]] = []
    seen: dict[str, str] = {}
    for name in wrapper_modules():
        module = importlib.import_module(f"chester.{name}")
        build = getattr(module, "build_tools", None)
        if build is None:
            raise RuntimeError(
                f"chester.{name} exports no `build_tools` — "
                "the wrapper layer has exactly one entry point."
            )
        for tool in build(workspace):
            if tool.__name__ in seen:
                raise RuntimeError(
                    f"duplicate tool name: {tool.__name__} "
                    f"({seen[tool.__name__]} and {name})"
                )
            seen[tool.__name__] = name
            tools.append(tool)
    return tools


def resolve_workspace(env: dict[str, str] | None = None) -> str:
    """The server's workspace — **absolute**, and independent of the start directory.

    `CHESTER_WORKSPACE` wins; otherwise the workspace sits next to the package, i.e.
    where Chester's agent keeps it too (one shared cache, as decided).

    **Why not simply `DEFAULT_WORKSPACE`:** it is *relative* (`.chester/workspace`)
    and therefore hangs on the process's working directory. Chester's agent is
    started from the project directory, an MCP server is not — Claude Desktop starts
    it with a working directory nobody chose. Measured 2026-09-14 with ``cwd="/"``:
    the server dies at startup on `'.chester/workspace'`. Loudly at least, but the
    answer to "where does it write?" must not be "depends on how it was started".

    Nobody else can answer the question either: **the client supplies no
    workspace.** MCP does know `roots`, but SEP-2577 removed server-initiated
    requests from the protocol — `ctx.list_roots()` is explicitly not part of the
    server API. The directory is decided at startup or not at all.
    """
    source = os.environ if env is None else env
    configured = source.get("CHESTER_WORKSPACE")
    if configured:
        return str(Path(configured).expanduser().resolve())
    return str((Path(__file__).resolve().parent.parent / DEFAULT_WORKSPACE).resolve())


#: Switch for **automatically** attaching the still image to every return value with
#: a `picture`. Default **off**, and that is a measurement decision: in F+ Chester's
#: agent does not see its map on its own either (the gate's visual check only runs
#: from level 2, the default is 1; it has to call `inspect_map`). Attached
#: automatically, F+MCP would get a free look that F+ does not have — and the
#: measurement would not know. Switch it on for product use; its setting belongs in
#: the run log.
ATTACH_ENV = "CHESTER_MCP_ATTACH_PICTURES"

#: A picture beyond this is no longer a picture but an accident — and an accident
#: does not belong in a foreign client's context.
MAX_PICTURE_BYTES = 5 * 1024 * 1024


def attach_pictures(env: dict[str, str] | None = None) -> bool:
    """Is the switch for automatically attached still images on?"""
    source = os.environ if env is None else env
    return str(source.get(ATTACH_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


def _as_image(raw: str, media_type: str):
    """Base64 plus media type → a protocol image block."""
    from mcp.types import ImageContent

    return ImageContent(type="image", data=raw, mime_type=media_type)


#: File the server appends every call to — one JSON object per line.
CALL_LOG = "mcp-calls.jsonl"


def _log_call(workspace: str, name: str, duration: float, result: Any) -> None:
    """Record one tool call. Never fatal — a log never costs a result.

    **Why the server has to do this itself.** Claude Desktop's MCP log records
    `method="tools/call"` and drops the parameters — never the tool *name* (checked
    2026-09-14). From outside only the *number* of calls is visible, not which ones.
    Cell F+MCP thereby lacked exactly the figure the bench records for L+ and F+:
    tool coverage. And questions like "did the model call `validate_result`?" — the
    core question of this cell — would stay unanswerable for good.

    Recorded is **what** was called and how it ended, not the payload: arguments can
    hold base64 images or whole geometries, and a log that grows with them soon logs
    nothing at all.
    """
    import json
    import time

    try:
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": name,
                "duration_s": round(duration, 3)}
        if isinstance(result, dict):
            line["ok"] = result.get("ok")
        with (Path(workspace) / CALL_LOG).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — a log never costs a result
        pass


def read_call_log(workspace: str) -> list[dict]:
    """The calls of a run, oldest first. Missing file → empty list."""
    import json

    path = Path(workspace) / CALL_LOG
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


def _with_picture(tool: Callable[..., dict], *, automatic: bool,
                  workspace: str = "") -> Callable[..., Any]:
    """Send pictures through the protocol — the one place where the adapter does more.

    Two paths, deliberately of different strictness:

    * **On request.** If the return value carries ``content_base64`` plus
      ``media_type`` (only `read_artifact` does), it is **always** sent as an image:
      the client asked for it explicitly. The base64 blob is dropped from the
      structured output — it travels in the image block, not twice.
    * **Automatic.** If the return value carries a ``picture`` (a path), the image is
      attached only when :data:`ATTACH_ENV` is set. Default off, see there.

    The structured output is kept in both cases; nothing a client could read before
    disappears.
    """
    import functools
    import time

    @functools.wraps(tool)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        result = tool(*args, **kwargs)
        if workspace:
            _log_call(workspace, tool.__name__, time.monotonic() - start, result)
        if not isinstance(result, dict):
            return result
        try:
            from fastmcp.tools import ToolResult
            from fastmcp.utilities.types import Image

            raw = result.get("content_base64")
            if raw and str(result.get("media_type", "")).startswith("image/"):
                lean = {k: v for k, v in result.items() if k != "content_base64"}
                return ToolResult(content=[_as_image(raw, result["media_type"])],
                                  structured_content=lean)

            picture = result.get("picture")
            if automatic and picture and Path(picture).is_file() \
                    and Path(picture).stat().st_size <= MAX_PICTURE_BYTES:
                return ToolResult(content=[Image(path=str(picture)).to_image_content()],
                                  structured_content=result)
        except Exception:  # noqa: BLE001 — a missing picture never costs the result
            return result
        return result

    return wrapper


def build_server(workspace: str = DEFAULT_WORKSPACE,
                 tools: list[Callable[..., dict]] | None = None):
    """A `FastMCP` server with Chester's geo tools, without instruction text.

    ``tools`` accepts an already collected list so the caller need not build it twice
    (the startup message reports the count).
    """
    from fastmcp import FastMCP

    server = FastMCP("chester")
    for tool in (collect_tools(workspace) if tools is None else tools):
        server.tool(_with_picture(tool, automatic=attach_pictures(),
                                  workspace=workspace))
    return server


def main(argv: list[str] | None = None) -> int:
    """Entry point — stdio, until the client closes the channel.

    Called as ``uv run python -m chester.mcpserver``. **No `[project.scripts]`:** this
    project deliberately has no `build-system`, uv runs it as a virtual project — a
    script entry would never be installed and would only look as if the command
    existed. `uv run` is the house form anyway (`ask.py`, `probe.py`).

    The workspace comes from `CHESTER_WORKSPACE` or is the usual one; it is created
    if missing. **No model, no provider, no `chester.json`** — whoever only wants the
    server does not need the agent's setup ceremony.

    Messages go to **stderr**: stdout is the protocol channel, and every character on
    it destroys the session.
    """
    del argv
    workspace = resolve_workspace()
    try:
        Path(workspace).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"chester-mcp: cannot create workspace {workspace} ({exc}). "
              "Set CHESTER_WORKSPACE to a writable directory.",
              file=sys.stderr)
        return 1
    tools = collect_tools(workspace)
    server = build_server(workspace, tools)
    print(f"chester-mcp: {len(tools)} tools, workspace {workspace}", file=sys.stderr)
    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
