"""The wrapper layer as one collection — every adapter gathers its tools here.

Moved out of `chester.mcpserver` (2026-09-19, KP.5 T2c): chester-team's ressort
agents need the same tools, and must not import chester-mcp to get them. Two rules
carry over unchanged, both learned the hard way:

* A module without `build_tools` is an **error**, not an omission: skipping it
  silently serves a smaller catalogue — what happened to `vectoroptools` while it
  was called `op_tools` (2026-09-14).
* ``chester`` is a namespace package over several distributions, so the modules are
  found across every portion of ``chester.__path__``, never next to one file.

``options`` threads configuration into the modules that take it (data roots and a
PostGIS DSN for `connectorstools`, extra STAC catalogues, cache retention); a module
not named there is built with its workspace alone, as Chester-MCP always has.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any


def wrapper_modules(exclude: frozenset[str] = frozenset()) -> list[str]:
    """Wrapper module names in catalogue order, across every namespace portion."""
    import chester

    stems = {p.stem for d in chester.__path__ for p in Path(d).glob("*tools.py")}
    return sorted(stems - exclude)


def collect_tools(
    workspace: str,
    *,
    exclude: frozenset[str] = frozenset(),
    options: dict[str, dict[str, Any]] | None = None,
    only: set[str] | None = None,
) -> list[Callable[..., dict]]:
    """Every tool of the wrapper layer, bound to ``workspace``; raises on a gap.

    ``only`` keeps just the named tools, in catalogue order — the one place that turns
    a set of names from `chester.ressortcut` into callables, so a ressort and the
    orchestrator cannot build that list two different ways.
    """
    tools: list[Callable[..., dict]] = []
    seen: dict[str, str] = {}
    for name in wrapper_modules(exclude):
        module = importlib.import_module(f"chester.{name}")
        build = getattr(module, "build_tools", None)
        if build is None:
            raise RuntimeError(
                f"chester.{name} exports no `build_tools` — "
                "the wrapper layer has exactly one entry point."
            )
        for tool in build(workspace, **(options or {}).get(name, {})):
            if tool.__name__ in seen:
                raise RuntimeError(
                    f"duplicate tool name: {tool.__name__} "
                    f"({seen[tool.__name__]} and {name})"
                )
            seen[tool.__name__] = name
            if only is None or tool.__name__ in only:
                tools.append(tool)
    return tools


def module_instructions(module: ModuleType) -> str:
    """A wrapper module's instruction text — ``instructions()`` if it has one, else
    ``INSTRUCTIONS``, else empty (the MCP-only modules carry none)."""
    fn = getattr(module, "instructions", None)
    return fn() if callable(fn) else str(getattr(module, "INSTRUCTIONS", ""))
