"""`geo_python_run` — the GeoPandas escape hatch, shared by every Chester agent.

Moved out of `chester.capabilities.vector` (2026-09-19, KP.5 T2): the vector and
raster ressorts of chester-team need it as much as chester-agent does — without
QGIS, turning a table into points or computing a Gini coefficient goes only through
a snippet. It lives in chester-runtime, not chester-geo-tools, because its guard
reads the run so far through `selmakit.tool_returns`: framework knowledge.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext

from chester import provenance
from chester.geo_python import hand_rolled_operations
from chester.ressortcut import ressort_of
from chester.workspace import resolve_path

#: Fingerprint of a refusal, so the next call can count it.
_GUARD_MARKER = "a checked function already does this"
_GUARD_MAX = 2


def _checked_route_guard(ctx, code: str,
                         available_tools: frozenset[str] | None = None) -> dict | None:
    """Refuse, **once**, a snippet that rebuilds a checked function.

    ``available_tools`` are the tool names the caller can actually reach — a ressort
    passes its own slice, the single agent passes nothing because it has them all. It
    decides *how* the refusal points at a route, not whether it refuses (see
    :func:`_refusal`).

    Measured 2026-09-06 (`buffer-schools-500m`, QGIS off): the agent found
    `geo_python_run` at once — and wrote raw geopandas in it three times. Correct on
    the merits, 84 buffers in EPSG:25832, 784,137 m² against 785,398 m² expected.
    And every assurance came to nothing: `outputs: []`, `calls: []`, **not one
    provenance sidecar**, no mixed-geometry note. The escape hatch had become the
    main road.

    **One round only**, learned from the PyQGIS guard's history: the second call of
    the same snippet runs. Some tasks have no checked function (a Gini coefficient,
    a kernel density), and a lock that pushes even then only costs rounds. Also
    capped: after `_GUARD_MAX` refusals in a run it stays silent.
    """
    hand_rolled = hand_rolled_operations(code or "")
    if not hand_rolled:
        return None
    if not getattr(ctx, "messages", None):
        return None  # direct call outside a run — there is no round to count
    try:
        from selmakit import tool_returns

        # Read in order: what counts is a refusal **since the last snippet that
        # ran**. If a single no opened the whole run, the lock would be useless after
        # one round — exactly what happened to the PyQGIS guard on 2026-08-27 (one
        # search, then twelve hand-written blocks). The cap above bounds what it can
        # contribute in total.
        refused_since_run = False
        refusals_total = 0
        for name, content in tool_returns(ctx):
            if name != "geo_python_run":
                continue
            is_refusal = (isinstance(content, dict)
                          and _GUARD_MARKER in str(content.get("error", "")))
            if is_refusal:
                refused_since_run = True
                refusals_total += 1
            else:
                refused_since_run = False  # the snippet ran — armed again
    except Exception:  # noqa: BLE001 - an unreadable context must never block the work
        return None
    if refused_since_run or refusals_total >= _GUARD_MAX:
        return None
    return {
        "ok": False,
        "error": _refusal(hand_rolled, available_tools),
        "checked_functions": [name for name, _ in hand_rolled],
    }


#: What every checked route buys, whichever way it is reached.
_WHY = ("They take and return paths, report what went in and out, refuse metric work "
        "in a geographic CRS and stamp provenance; a hand-rolled equivalent returns "
        "`outputs: []`, writes no sidecar, and leaves the validation gate blind to "
        "what you produced.")


def _refusal(hand_rolled: list[tuple[str, str]],
             available_tools: frozenset[str] | None) -> str:
    """The refusal text, split by **how the receiver can actually reach** each route.

    Until 2026-09-27 this said "These are **tools** — call them directly" about every
    name it found. That was wrong twice over, and a Test-Level-3 run paid for it
    (`mean-elevation-per-district`): `read_vector` and `write_vector` are not tools at
    all but snippet bindings, and `zonal_stats` is a tool of the *raster* ressort, which
    is not the one that was being told to call it. The ressort followed the advice into
    a dead end — snippet refused, tool absent — and then rebuilt zonal statistics by
    hand with `rasterio.mask(filled=True, nodata=0)`, which averaged the zeros outside
    each polygon into the mean: eighteen districts, eighteen wrong elevations, `ok:
    true`. The operation this guard names would have masked them out.

    So each name goes where it can be reached: as a tool, or as the same checked
    implementation already bound in the snippet.
    """
    as_tool = [(n, g) for n, g in hand_rolled
               if available_tools is None or n in available_tools]
    in_snippet = [(n, g) for n, g in hand_rolled
                  if (available_tools is not None and n not in available_tools)
                  or n in {"read_vector", "write_vector"}]
    as_tool = [(n, g) for n, g in as_tool if (n, g) not in in_snippet]
    parts = [f"{_GUARD_MARKER}:"]
    if as_tool:
        listed = "; ".join(f"`{n}` — {g}" for n, g in as_tool)
        parts.append(f"{listed}. These are **tools** — call them directly, one call "
                     f"per step, instead of writing a snippet. {_WHY}")
    for name, gain in in_snippet:
        owner = ressort_of(name) if available_tools is not None else None
        where = f" (a tool of the {owner} ressort)" if owner else ""
        parts.append(
            f"`{name}` — {gain} — is **already bound in this snippet**{where}: call "
            f"`{name}(...)` without importing it, and you get the checked "
            "implementation with its provenance and its `calls` entry. Rebuilding it "
            "by hand is the one thing to avoid; if the task needs a tool this ressort "
            "does not have, say so in your handover instead.")
    parts.append("Or call this again with the same code and it will run: for anything "
                 "the checked routes do not cover, that is the right answer.")
    return " ".join(parts)


def build_geo_python_run(ws: str,
                         available_tools: frozenset[str] | None = None,
                         ) -> Callable[..., dict]:
    """The `geo_python_run` tool, bound to workspace ``ws``.

    ``available_tools``: the names the receiving agent can call as tools. A ressort
    passes its own slice so the guard cannot send it to a tool it does not have; the
    single agent leaves it out, having the whole surface.
    """

    def geo_python_run(ctx: RunContext[Any], code: str,
                       timeout_seconds: int = 300) -> dict:
        """Run a GeoPandas snippet when no named tool fits.

        The namespace already holds `gpd`/`pd`/`np`, `shapely` (with `Point`,
        `Polygon`, `box`, …), `pyproj` and `rasterio` — imports are allowed but
        never needed. Assign a JSON-serialisable value to `result` to return it;
        `print(...)` is captured as `stdout`.

        Read and write through the two injected helpers rather than
        `gpd.read_file`/`to_file`: `read_vector(path)` collapses every path
        spelling and warns about a mixed-geometry layer before you compute on it,
        `write_vector(gdf, path)` puts the file in the GeoCache, stamps its
        provenance and reports it back. Both are recorded in `calls`.

        For a single standard operation prefer the named tools (`vector_filter`,
        `vector_overlay`, `vector_split_by_geometry`, `qgis_run` when QGIS is
        present) — they carry their own checks. This is the escape hatch for what
        none of them expresses.
        """
        from chester.geo_python import GeoPythonError, run_geo_python

        hand_rolled = _checked_route_guard(ctx, code, available_tools)
        if hand_rolled:
            return hand_rolled
        cache_dir = Path(resolve_path("x.gpkg", ws)).parent
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            verdict = run_geo_python(code, cwd=str(cache_dir), timeout=timeout_seconds)
        except GeoPythonError as exc:
            return {"ok": False, "error": str(exc)}

        for path in verdict.get("outputs") or []:
            provenance.write_meta(
                path, source="chester", tool="geo_python_run", query=code
            )
        if not verdict.get("ok"):
            return {"ok": False, "error": verdict.get("error") or "unknown error",
                    "stdout": verdict.get("stdout") or "",
                    "calls": verdict.get("calls") or []}
        return {
            "ok": True,
            "result": verdict.get("result"),
            "stdout": verdict.get("stdout") or "",
            "outputs": verdict.get("outputs") or [],
            # The calls of the checked helpers sit in the return **content** on
            # purpose. `selmakit.tool_returns` reads `part.content` and drops
            # `part.metadata`; what a subprocess does would otherwise be invisible
            # there and the gate would fail silently — the defect found in
            # CodeMode on 2026-09-06.
            "calls": verdict.get("calls") or [],
        }

    return geo_python_run
