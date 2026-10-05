"""GeoPyCapability — run arbitrary PyQGIS code headless.

The escape hatch for GIS computation the algorithm tools can't express: a single
``qgis_python(code)`` tool that runs a PyQGIS snippet in QGIS's own bundled
Python (via :mod:`chester.qgis_python`), with ``processing`` and the ``qgis.*``
modules available. Same execution boundary as ``qgis_run`` — the same local,
single-user trust level — so, unlike ``qgis_show``, it needs no ask-first gate.

Output confinement: the snippet runs with its CWD set to the GeoCache dir, so a
bare output filename lands in the inventoried, self-expiring cache. Any output
path returned via the snippet's ``result`` is stamped with a ``chester``
provenance sidecar, exactly like a QGIS-algorithm output.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import provenance
from chester.qgis_process import QgisProcess
from chester.qgis_python import QgisPythonError, run_pyqgis
from chester.workspace import DEFAULT_WORKSPACE, GEOCACHE_SUBDIR

_INSTRUCTIONS = """\
## Custom PyQGIS (qgis_python)

`qgis_python(code)` runs an arbitrary **PyQGIS** snippet headless in QGIS's own
Python. Use it only when the QGIS toolbox tools don't fit — multi-step
computation, iterating over features, custom geometry/attribute math, or an
algorithm chain that would be clumsy as separate calls. For a single algorithm,
prefer `qgis_run` / the named shortcuts.

In the snippet you may:
- call `processing.run("native:...", {...})` and use any `Qgs*` class straight
  away — `processing` and all of `qgis.core` are **already in the namespace**,
  like in QGIS's Python console. Imports are allowed but never necessary.
- `print(...)` for logs — captured and returned as `stdout`.
- Assign a JSON-serialisable value (number, string, list, dict) to a variable
  named `result` to return it. **Return any output file path(s) you write in
  `result`** so they are inventoried and get provenance.

Reading inputs: the working directory is the GeoCache, so a cached dataset is a
**bare filename** (`"buildings.gpkg"`), NOT the `.chester/workspace/geocache/…`
path other tools print. Do not pass that prefixed path to `QgsVectorLayer` (the
layer comes back silently invalid) and never `os.path.abspath` it (the CWD is
already the cache, so it doubles the path). A `resolve_path(name)` helper is
pre-injected — call it on ANY input path and it collapses every spelling
(`".chester/workspace/geocache/x.gpkg"`, `"geocache/x.gpkg"`, `"x.gpkg"`) to the
right cache file: `layer = QgsVectorLayer(resolve_path(path), "in", "ogr")`.

Output files: writing a bare filename (e.g. `OUTPUT="clipped.gpkg"`) lands it in
the cache.

Geometry types: `QgsGeometry.type()` returns a `QgsWkbTypes.GeometryType` —
**0 = Point, 1 = Line, 2 = Polygon, 3 = Unknown, 4 = Null** (polygon is 2, not
3; testing `== 3` matches nothing and silently yields zero). Use
`QgsWkbTypes.geometryDisplayString(geom.type())` if unsure. For a vertex count,
`geom.constGet().nCoordinates()` handles single/multi parts without manual
`asPolygon()`/`asMultiPolygon()` branching (note a closed ring repeats its first
point as the last, so a simple rectangle counts as 5).

The tool returns `{"ok": true, "result": ..., "stdout": ..., "outputs": [...]}`,
or `{"ok": false, "error": <traceback>}` on failure — read the traceback and fix
the snippet, don't repeat the same call.

**Before you write a snippet, check the named tool** — the `qgis_python` docstring
lists which one replaces which snippet, and the error hint repeats it when a call
fails. A run that spent fifteen of its twenty-four calls here did filtering,
reprojection, a spatial selection and four "list the values of a column" loops —
every one of them a single named call.\
"""
# The snippet→tool table itself deliberately lives in the `qgis_python` docstring
# (where the model looks while *choosing* a tool) and in `_ERROR_HINT` (a return
# value, which this project's own evidence says beats prompt text). Having it here a
# third time cost ~500 characters of every prompt and changed nothing: qgis_python is
# still the most-called tool of the whole bank, 96 calls over 24 runs. Removed
# 2026-08-23 (K.2). Do not paste it back — extend the docstring instead.

# Failures whose cause is usually a name the snippet did not need to look up.
_NAMESPACE_ERRORS = ("NameError", "ImportError", "AttributeError")

_ERROR_HINT = (
    "processing, every Qgs* class AND the resolve_path(name) helper are already in "
    "the namespace — no import needed. resolve_path is Chester's, not QGIS's: "
    "`from qgis.core import resolve_path` raises ImportError (seen 2026-08-23, "
    "buffer-schools-500m), just call it. "
    "If this snippet filters, selects by location, reprojects, clips, rasterizes or "
    "lists a column's values, a named tool does it in one call without code: "
    "vector_filter, vector_extract_by_attribute, vector_extract_by_location, "
    'vector_reproject, vector_clip, rasterize, vector_info(path, values_of="name"). '
    "Two references worth consulting before writing more code — `web_fetch(url)` "
    "opens them, so this is a step you can actually take, and the algorithm you "
    "need probably exists: the QGIS processing algorithms are documented at "
    "https://docs.qgis.org/latest/en/docs/user_manual/processing_algs/index.html "
    "(vector→raster and friends under gdal/vectorconversion.html), and the PyQGIS "
    "classes at https://qgis.org/pyqgis/master/ with the cookbook at "
    "https://docs.qgis.org/latest/en/docs/pyqgis_developer_cookbook/index.html."
)


def _collect_output_paths(result: Any, cache_dir: str) -> list[str]:
    """Return existing file paths referenced in ``result`` (str / list / dict).

    Relative paths are joined to ``cache_dir`` (the snippet's CWD), so a bare
    output filename returned by the snippet resolves to the file it wrote.
    """
    found: list[str] = []

    def _add(value: Any) -> None:
        if not isinstance(value, str) or not value:
            return
        full = value if os.path.isabs(value) else os.path.join(cache_dir, value)
        if os.path.isfile(full) and full not in found:
            found.append(full)

    if isinstance(result, str):
        _add(result)
    elif isinstance(result, list):
        for item in result:
            _add(item)
    elif isinstance(result, dict):
        for item in result.values():
            if isinstance(item, list):
                for sub in item:
                    _add(sub)
            else:
                _add(item)
    return found


# The refusal's own fingerprint, so a later call can count how often it has fired
# without re-parsing prose. Two refusals are the ceiling (see `_search_first`).
_REFUSAL_MARKER = "no algorithm search happened in this run yet"
_MAX_REFUSALS = 3

#: Words that stand in every PyQGIS snippet and say nothing about the task.
_BOILERPLATE = frozenset("""
    import from for while if else elif try except return result none true false
    qgis core gui analysis processing utils context feedback params provider
    print len str int float list dict set sorted range open path os sys json
    processing run qgs qgsvectorlayer qgsproject qgsfeature qgsgeometry layer
    layers feature features field fields value values path paths file output input
    resolve_path getfeatures isvalid append not and or in is def class self none
""".split())

#: Stands in the refusal and tells the next call: the search has run.
_SEARCHED_MARKER = "searched-on-your-behalf"

# The hand-written return cap that stood here (`_MAX_RETURN_CHARS = 4000`) went away on
# 2026-09-06: `pydantic_ai_harness.tool_output_limits` does it better and for **every**
# tool. Above 10,000 characters the full return moves to a store, the model gets a
# preview plus a handle and reads on with `read_tool_result` — instead of the lossy cut
# that stood here. Two caps with different thresholds would be worse than one: the
# smaller wins and prevents the offloading the larger is built for. Wired in
# `agent_build.geo_capabilities()`.


#: What makes a snippet the guard's business. It exists to redirect hand-rolled
#: *geoprocessing* onto a ready-made algorithm — so a snippet that touches no spatial
#: API is outside its remit, and there is nothing for `qgis_search` to find.
_GEO_MARKERS = ("qgs", "processing", "iface", "gdal", "ogr", "osgeo",
                "shapely", "geopandas", "rasterio", "geometry", "crs")


def _is_geoprocessing(code: str) -> bool:
    """Whether this snippet does spatial work at all.

    Measured 2026-09-05 (`points-from-a-table`, Test-Level 2): three of the run's
    five `qgis_python` calls were refused, and not one of them was geoprocessing —
    ``os.listdir('.')`` and reading a CSV header twice. No algorithm exists for
    either, so being told to search first was noise; worse, it taught the model to
    spend a round on a throwaway snippet (``result = 1 + 1``) to open the gate, and
    that spent the pass the real snippet needed. The order rule is worth its cost
    only where a ready-made route can exist.

    Deliberately generous in the other direction: anything naming a spatial library
    or a Qgs* class counts, so the cases the guard was built for — eleven snippets
    rebuilding `native:rastersampling` — stay firmly inside it.
    """
    return any(marker in (code or "").lower() for marker in _GEO_MARKERS)


def _likely_keywords(code: str, limit: int = 3) -> list[str]:
    """The words in a snippet worth searching for.

    Deliberately coarse: import lines go entirely (they name QGIS modules, not the
    task); of the rest the identifiers stay, without the scaffolding every snippet
    has, sorted by frequency — what a snippet touches often is more likely its subject
    than its frame. The list need not be clever; it must be good enough that the
    refusal carries **information** instead of an instruction.
    """
    import re
    from collections import Counter

    body = "\n".join(line for line in (code or "").splitlines()
                     if not line.lstrip().startswith(("import ", "from ")))
    words = [w.strip("_").lower() for w in re.findall(r"[A-Za-zÄÖÜäöü_]{4,}", body)]
    counted = Counter(w for w in words if len(w) >= 4 and w not in _BOILERPLATE)
    return [w for w, _n in counted.most_common(limit)]


def _search_first(ctx: Any, code: str = "", search: Any = None) -> dict | None:
    """Refuse the first snippet of a run that never looked for an algorithm.

    "Last resort, not first reach" has stood in this docstring, in `_ERROR_HINT`
    and (until 2026-08-23) in the instructions, and it did not work: `qgis_python`
    was the most-called tool of the whole bank, 96 calls over 24 runs. On
    2026-08-23 `viewpoints-above-400m` spent **eleven consecutive** snippets
    hand-rolling what `native:rastersampling` does in one call — and the single
    `qgis_search` it did make that run was for "hillshade", a different question
    entirely. The model does not reject searching; it just does not think of it
    while *computing*.

    So the order is enforced instead of advised: the cost is one extra call for a
    run that would have written a snippet blind, and nothing for a run that already
    looked.

    **The gate re-arms after every executed snippet** (2026-08-30). Until then *one*
    search anywhere in the run lifted it for good — exactly what happened on
    2026-08-27 in session `553e7483`: one search for "buffer", then **twelve more**
    hand-written PyQGIS blocks assembling a point layer from four addresses and ending
    in an empty raster. For `gdal:rasterize` — the first hit of
    `qgis_search("rasterize")` — that run never searched. Required now is a search
    **since the last executed snippet**, not some time in the run.

    **Bounded on purpose.** After ``_MAX_REFUSALS`` refusals **in the whole run** the
    snippet runs unsearched. The count was briefly switched to *consecutive* refusals
    and reverted on 2026-09-01: in the dialogue case "mark four addresses" it refused
    **ten** of sixteen `qgis_python` calls — the regular route
    (`native:createpointslayerfromtable`) crashed back then, so there was nothing to
    find, and the gate turned a fixable error into a dead end. A guard that keeps
    pushing when the recommended route is broken only costs time. Three refusals are
    the most it can contribute.
    A gate without a ceiling works against a model whose stubbornness
    you cannot know — and this project has already lost one run to a loop that ended
    at the request limit (2026-08-23, `gtfs-stops-departures-map-regensburg`, a
    different cause and the same shape). Measured the same day: two refusals were
    enough for `gemma4:26b-mlx` to route around via `vector_info`, so the ceiling
    costs nothing that was working and removes a failure mode that was possible.
    """
    # Outside the guard's remit: no spatial API in the snippet, so no ready-made
    # route can exist and demanding a search first would only cost a round.
    if not _is_geoprocessing(code or ""):
        return None
    # No conversation behind the call — a direct/unit invocation, not a run. There
    # is nothing to have searched *in*, so the order cannot be judged and is allowed.
    if not getattr(ctx, "messages", None):
        return None
    try:
        from selmakit import tool_returns

        returns = tool_returns(ctx)
        # Read in order: what counts is a search **since the last executed snippet** —
        # not any search at some point in the run.
        searched_since = False
        refused_total = 0
        for name, content in returns:
            is_refusal = (
                name == "qgis_python"
                and isinstance(content, dict)
                and _REFUSAL_MARKER in str(content.get("error", ""))
            )
            if is_refusal:
                refused_total += 1
                # The refusal ran the search **itself**. That meets the condition
                # and the next call runs — otherwise "call it again and it will run"
                # would simply be untrue: until 2026-09-01 the second and third
                # attempt with the same text were refused again, and the run lost
                # three rounds to a guard that would not open.
                if _SEARCHED_MARKER in str(content.get("searched", "")):
                    searched_since = True
            elif name == "qgis_python":
                searched_since = False  # the snippet ran — the gate re-arms
            elif name in ("qgis_search", "qgis_describe"):
                searched_since = True
    except Exception:  # noqa: BLE001 - context we cannot read → never block the work
        return None
    # Two rules that belong together: the gate re-arms after every executed snippet
    # (otherwise one search lifts it for the whole run), but it speaks up at most
    # `_MAX_REFUSALS` times per run.
    if searched_since or refused_total >= _MAX_REFUSALS:
        return None
    # Deliver the search along with the refusal instead of demanding a round for it.
    # Measured 2026-09-01 (`height-gini`): **one** call, a finished snippet, refused —
    # and the second round no longer fit the time limit. QGIS has no procedure for a
    # Gini coefficient, so the demanded search was bound to come back empty. A guard
    # that demands information it could give itself only costs time.
    words = _likely_keywords(code or "")
    hits: list[dict] = []
    if search is not None and words:
        try:
            for word in words:
                for hit in search(word)[:3]:
                    if hit.get("id") and not any(h["id"] == hit["id"] for h in hits):
                        hits.append({"id": hit["id"], "name": hit.get("name")})
        except Exception:  # noqa: BLE001 - information must never make the call raise
            hits = []
    if hits:
        found = "; ".join(f"{h['id']} ({h['name']})" for h in hits[:6])
        outcome = (f"I searched the toolbox for {', '.join(words)} on your behalf: "
                   f"{found}. If one of these does the job, run it with qgis_run — "
                   "it carries the parameter checks. If none fits, call qgis_python "
                   "again with the same code and it will run.")
    else:
        # Zero hits must **not** unlock. The search words come from the snippet's
        # identifiers, i.e. from what the model itself typed — not from the task.
        # Measured 2026-09-05 in `join-leading-zero-ags`: searched for `v_layer,
        # temp_layer, einwohner`, nothing found, and the refusal ended with "so a
        # snippet is the right route here". The task was a **join**; a `qgis_search`
        # for "join" would have returned `native:joinattributestable`, which the prompt
        # prescribes for statistics joins. The information had become a permission,
        # and the run hand-wrote the join for ten calls until the time limit fell.
        outcome = (f"I searched the toolbox for {', '.join(words) or 'this'} on your "
                   "behalf and found nothing — but that says little: those words come "
                   "from the identifiers in YOUR snippet, not from the task. Before "
                   "writing this by hand, run one `qgis_search` with a word for the "
                   "*operation* (join, clip, buffer, sample, rasterize, zonal …) or "
                   "for the thing you want out. If that finds nothing either, call "
                   "qgis_python again with the same code and it will run.")
    return {
        "ok": False,
        "error": (
            f"{_REFUSAL_MARKER}. A ready-made route is cheaper and cannot fail on an "
            "invented API: a Chester tool built for the job (`spectral_index`, "
            "`sample_raster`, `vector_add_field`, `vector_field_sum`, "
            "`rasterize`), or one of QGIS's ~761 algorithms via `qgis_run`. "
            f"{outcome}"
        ),
        "searched": f"{_SEARCHED_MARKER}: {', '.join(words) or '(no keyword)'}",
        "candidates": hits[:6],
        "hint": _ERROR_HINT,
    }


@dataclass
class GeoPyCapability(AbstractCapability[Any]):
    """Expose a headless PyQGIS runner as a single LLM tool."""

    workspace: str = DEFAULT_WORKSPACE
    timeout: int = 300

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace
        timeout = self.timeout
        # Only for the information in the refusal — the catalogue is read once on first
        # access and then kept in the process.
        catalog = QgisProcess()

        def qgis_python(ctx: RunContext[Any], code: str) -> dict:
            """Run an arbitrary PyQGIS snippet headless and return its result.

            ``processing`` and every ``Qgs*`` class are already in the namespace —
            no imports needed. Assign a JSON-serialisable value to a variable
            ``result`` to return it, and put any output file path(s) you write in
            ``result`` so they are inventoried. Outputs land in the workspace cache.

            **Last resort, not first reach.** A named tool is one call and cannot
            fail on a hallucinated API: attribute filter → ``vector_filter`` /
            ``vector_extract_by_attribute``; spatial selection →
            ``vector_extract_by_location``; reprojection → ``vector_reproject``; clip →
            ``vector_clip``; layer facts → ``vector_info``; a column's values →
            ``vector_info(path, values_of="name")``; a single algorithm →
            ``qgis_run``. Write a snippet only for what none of them covers.
            """
            unsearched = _search_first(ctx, code, catalog.search)
            if unsearched:
                return unsearched
            cache_dir = Path(ws) / GEOCACHE_SUBDIR
            cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                verdict = run_pyqgis(code, cwd=str(cache_dir), timeout=timeout)
            except QgisPythonError as exc:
                return {"ok": False, "error": str(exc)}

            if not verdict.get("ok"):
                error = verdict.get("error") or "unknown PyQGIS error"
                failed = {
                    "ok": False,
                    "error": error,
                    "stdout": verdict.get("stdout") or "",
                }
                # A missing or invented name is the one failure the tool can talk the
                # model out of repeating — `vector_filter` does the same with its
                # column list. Everything else (a real bug in the snippet) is the
                # traceback's job.
                if any(kind in error for kind in _NAMESPACE_ERRORS):
                    failed["hint"] = _ERROR_HINT
                return failed

            outputs = _collect_output_paths(verdict.get("result"), str(cache_dir))
            for path in outputs:
                provenance.write_meta(path, source="chester", tool="qgis_python", query=code)
            return {
                "ok": True,
"result": verdict.get("result"),
                "stdout": verdict.get("stdout") or "",
                "outputs": outputs,
            }

        return FunctionToolset(tools=[qgis_python])
