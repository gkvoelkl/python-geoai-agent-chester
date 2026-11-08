"""Static checks on a `run_code` program before it runs (Phase SP, after GeoContra).

GeoContra (arXiv 2605.00782) checks LLM-written GIS programs in three layers — static,
runtime, semantic — and feeds each violation back with its line. Its largest gains were
for small local models (Qwen3-14B 7.7 % → 47.3 % spatially correct). Chester's gate
already covers the runtime and semantic layers; this module is the static one. It only
exists for chester-code: there a whole task arrives as one program, so the chain is
readable *before* any data is fetched.

**A rule speaks only when it is sure.** The data flow is read off the syntax tree for the
plain patterns the coder writes (``x = await tool(...)``, ``x["output"]``); anything it
cannot follow — a loop, a helper function, a path built as a string — yields no verdict.
A false alarm blocks a correct program; a missed defect still meets the gate.

Four rules, chosen from measured failures and the rules the tools teach:

* **metric after reprojection** — ``vector_buffer`` on a layer that comes, unchanged, from
  a tool writing EPSG:4326 degrees;
* **polygons kept** — after ``vector_split_by_geometry`` only point families are selected
  (2026-10-25: GLM and Nemotron buffered 25 school points and dropped 59 school buildings;
  2026-11-07, A/B with Nemotron: without the check 25 schools, with it 84);
* **named place clipped** — ``osm_features`` gets the bbox of a geocoded *place* and nothing
  in the program clips: a bbox is a rectangle and pulls in the neighbours;
* **travel time is no buffer** — the task speaks of minutes, walking or reach, and the
  program buffers without ``service_area``. Needs the task text, so `check` takes it.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import ModelRetry
from pydantic_ai.capabilities import AbstractCapability

_RUN_CODE = "run_code"

#: Tools whose written layer is EPSG:4326 (degrees) — see their result types in
#: `chester.toolreturns`. A test holds this set against those descriptions.
GEOGRAPHIC_OUTPUT = frozenset({"osm_features", "fetch_dem", "fetch_gtfs_stops",
                               "fetch_gtfs_routes"})

#: Tools that write their input's CRS unchanged, and the parameter carrying that input.
CRS_PRESERVING = {"vector_clip": "input_path", "vector_filter": "path"}

#: Metric operations and the parameter holding the layer they measure.
METRIC_OPS = {"vector_buffer": "input_path"}

_POINT_FAMILIES = frozenset({"Point", "MultiPoint"})
_AREA_FAMILIES = frozenset({"Polygon", "MultiPolygon"})


@dataclass(frozen=True)
class Finding:
    """One rule violation: where, what, and the fix to write instead."""

    rule: str
    line: int
    message: str

    def render(self) -> str:
        return f"line {self.line}: {self.message}"


def _awaited_call(node: ast.AST) -> ast.Call | None:
    if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
        if isinstance(node.value.func, ast.Name):
            return node.value
    return None


def _name(call: ast.Call) -> str:
    """The called tool's name — `_awaited_call` only returns calls of a plain name."""
    return call.func.id if isinstance(call.func, ast.Name) else ""


def _calls(tree: ast.AST) -> dict[str, tuple[str, ast.Call]]:
    """``variable → (tool, call)`` for every ``variable = await tool(...)``."""
    out: dict[str, tuple[str, ast.Call]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, call = node.targets[0], _awaited_call(node.value)
            if isinstance(target, ast.Name) and call is not None:
                out[target.id] = (_name(call), call)
    return out


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _origin(expr: ast.expr | None, calls: dict, depth: int = 0) -> str | None:
    """The CRS kind of a layer expression: "geographic", "metric", or None (unknown)."""
    if expr is None or depth > 8:
        return None
    if not (isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name)
            and isinstance(expr.slice, ast.Constant) and expr.slice.value == "output"):
        return None
    found = calls.get(expr.value.id)
    if found is None:
        return None
    tool, call = found
    if tool in GEOGRAPHIC_OUTPUT:
        return "geographic"
    if tool == "vector_reproject":
        target = _kwarg(call, "target_crs")
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            return "geographic" if target.value.upper() in ("EPSG:4326", "WGS84") else "metric"
        return None
    if tool in CRS_PRESERVING:
        return _origin(_kwarg(call, CRS_PRESERVING[tool]), calls, depth + 1)
    return None


def metric_after_reprojection(tree: ast.AST) -> list[Finding]:
    calls = _calls(tree)
    out = []
    for node in ast.walk(tree):
        call = _awaited_call(node)
        if call is None or _name(call) not in METRIC_OPS:
            continue
        tool = _name(call)
        layer = _kwarg(call, METRIC_OPS[tool])
        if _origin(layer, calls) == "geographic":
            src = ast.unparse(layer) if layer is not None else "?"
            out.append(Finding("metric-after-reprojection", call.lineno,
                               f"`{tool}` measures `{src}`, which is EPSG:4326 (degrees) — "
                               "reproject it first with `vector_reproject(..., "
                               "target_crs=\"EPSG:25832\")` and pass that result's "
                               "\"output\"."))
    return out


def _geometry_type_literals(tree: ast.AST) -> set[str]:
    """String constants compared with a ``...["geometry_type"]`` anywhere in the program."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        sides = [node.left, *node.comparators]
        if not any(isinstance(s, ast.Subscript) and isinstance(s.slice, ast.Constant)
                   and s.slice.value == "geometry_type" for s in sides):
            continue
        for s in sides:
            for c in ast.walk(s):
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    out.add(c.value)
    return out - {"geometry_type"}


def polygons_kept(tree: ast.AST) -> list[Finding]:
    splits = [c for c in map(_awaited_call, ast.walk(tree))
              if c is not None and _name(c) == "vector_split_by_geometry"]
    if not splits:
        return []
    chosen = _geometry_type_literals(tree)
    if not chosen or not chosen <= _POINT_FAMILIES:
        return []
    return [Finding("polygons-kept", splits[0].lineno,
                    "after `vector_split_by_geometry` only point parts are used "
                    f"({', '.join(sorted(chosen))}) — polygon parts are dropped. Schools, "
                    "shops and stations are often mapped as buildings: keep "
                    f"{' and '.join(sorted(_AREA_FAMILIES))} too and join them with "
                    "`vector_merge`.")]


#: Operations that cut a layer to an area — any of them means the program clips somewhere.
_CLIPPING = frozenset({"vector_clip", "vector_intersection", "vector_extract_by_location"})


def _subscript_root(expr: ast.expr | None, aliases: Mapping[str, ast.expr]) -> str | None:
    """``place`` for ``place["bbox"]``, also through ``bbox = place["bbox"]``; else None."""
    for _ in range(4):
        if isinstance(expr, ast.Name) and expr.id in aliases:
            expr = aliases[expr.id]
        elif isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name):
            return expr.value.id
        else:
            return None
    return None


def named_place_clipped(tree: ast.AST) -> list[Finding]:
    calls = _calls(tree)
    aliases = {n.targets[0].id: n.value for n in ast.walk(tree)
               if isinstance(n, ast.Assign) and len(n.targets) == 1
               and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Subscript)}
    if any(_name(c) in _CLIPPING for c in map(_awaited_call, ast.walk(tree))
           if c is not None):
        return []  # some clip exists — whether it is the right one is the gate's question
    out = []
    for node in ast.walk(tree):
        call = _awaited_call(node)
        if call is None or _name(call) != "osm_features" or _kwarg(call, "place"):
            continue
        name = _subscript_root(_kwarg(call, "bbox"), aliases)
        if name is None or calls.get(name, ("",))[0] != "geocode":
            continue  # a literal or unknown bbox may be meant as a window
        out.append(Finding("named-place-clipped", call.lineno,
                           "`osm_features` takes the bbox of a geocoded place, and nothing in "
                           "the program clips — a bbox is a rectangle and pulls in the "
                           "neighbouring places. Pass `place=\"<name>\"` instead, or clip the "
                           "result with `fetch_boundaries` + `vector_clip`."))
    return out


_TRAVEL_TIME = re.compile(
    r"\d+\s*-?\s*(min\b|minute|minuten)|gehminut|fu(ß|ss)weg|zu fu(ß|ss)|fahrzeit|reisezeit|"
    r"\bwalk|travel.?time|isochron|erreichbar|reachable", re.IGNORECASE)


def travel_time_not_buffer(tree: ast.AST, task: str) -> list[Finding]:
    if not task or not _TRAVEL_TIME.search(task):
        return []
    used = [c for c in map(_awaited_call, ast.walk(tree)) if c is not None]
    if any(_name(c) == "service_area" for c in used):
        return []
    buffers = [c for c in used if _name(c) == "vector_buffer"]
    if not buffers:
        return []
    return [Finding("travel-time-not-buffer", buffers[0].lineno,
                    "the task asks for a travel time or reach, and the program draws a "
                    "`vector_buffer` — a radius ignores rivers, railways and dead ends. Use "
                    "`service_area` on the street network (`osm_features(tags={\"highway\": "
                    "True})`, reprojected to a metric CRS). No network, no answer: say so "
                    "instead of substituting a radius.")]


RULES = (metric_after_reprojection, polygons_kept, named_place_clipped)


def check(code: str, task: str = "") -> list[Finding]:
    """Every finding in ``code`` for ``task``; empty for a clean or unparsable program."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # the sandbox reports that itself
    return [*(f for rule in RULES for f in rule(tree)), *travel_time_not_buffer(tree, task)]


@dataclass
class ProgramCheckCapability(AbstractCapability[Any]):
    """Stops a `run_code` program with a finding before it runs — once per finding.

    A finding comes back as a retry naming the line and the fix. If the corrected program
    still trips the same rule, it runs anyway: the check may be wrong, and the gate still
    looks at the result. That bounds a false alarm to one extra round.
    """

    _reported: dict[str, set[str]] = field(default_factory=dict, repr=False)

    def get_instructions(self):
        return None  # the correction arrives as a retry, where it cannot be skipped

    async def before_tool_execute(self, ctx, *, call, tool_def, args):
        if tool_def.name != _RUN_CODE or not isinstance(args, dict):
            return args
        from chester.code.planner import prompt_text

        findings = check(str(args.get("code") or ""), prompt_text(ctx.prompt))
        run = str(getattr(ctx, "run_id", None) or id(ctx.deps))
        seen = self._reported.setdefault(run, set())
        new = [f for f in findings if f.rule not in seen]
        if not new:
            return args
        seen.update(f.rule for f in new)
        raise ModelRetry("This program was not run — a static check found:\n"
                         + "\n".join(f"- {f.render()}" for f in new)
                         + "\nFix these lines and send the whole corrected program again.")
