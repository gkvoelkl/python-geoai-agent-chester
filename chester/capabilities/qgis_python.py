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
- `import processing` (already available) and call `processing.run("native:...",
  {...})`, and `from qgis.core import QgsVectorLayer, QgsProject, ...`.
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
the snippet, don't repeat the same call.\
"""


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

        def qgis_python(code: str) -> dict:
            """Run an arbitrary PyQGIS snippet headless and return its result.

            ``processing`` and the ``qgis.*`` modules are available. Assign a
            JSON-serialisable value to a variable ``result`` to return it, and
            put any output file path(s) you write in ``result`` so they are
            inventoried. Outputs land in the workspace cache. Use this only when
            the named QGIS tools / qgis_run don't fit.
            """
            cache_dir = Path(ws) / GEOCACHE_SUBDIR
            cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                verdict = run_pyqgis(code, cwd=str(cache_dir), timeout=timeout)
            except QgisPythonError as exc:
                return {"ok": False, "error": str(exc)}

            if not verdict.get("ok"):
                return {
                    "ok": False,
                    "error": verdict.get("error") or "unknown PyQGIS error",
                    "stdout": verdict.get("stdout") or "",
                }

            outputs = _collect_output_paths(verdict.get("result"), str(cache_dir))
            for path in outputs:
                provenance.write_meta(
                    path, source="chester", tool="qgis_python", query=code
                )
            return {
                "ok": True,
                "result": verdict.get("result"),
                "stdout": verdict.get("stdout") or "",
                "outputs": outputs,
            }

        return FunctionToolset(tools=[qgis_python])
