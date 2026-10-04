"""Run a GeoPandas snippet in a subprocess of Chester's own interpreter.

The sibling of :mod:`chester.qgis_python`. That one exists because PyQGIS must never
be imported into this venv, so the snippet has to run in QGIS's interpreter; here the
libraries are already present and the subprocess is a deliberate choice, not a
necessity:

* the timeout is enforceable (a runaway snippet is killed, not waited on),
* a segfault in GDAL/GEOS ends the child, not the agent,
* the snippet cannot mutate Chester's process state — no stray ``sys.path`` entry, no
  matplotlib backend switched under the map renderer, no ``os.chdir``.

Phase KQ step 1: the mechanism of `qgis_python`, for a namespace needing no QGIS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT = 300  # seconds; a snippet may read several large layers

_HARNESS = Path(__file__).resolve().parent / "resources" / "geo_python_harness.py"
#: The harness imports `chester.geofacts`; its CWD is the GeoCache, so this `chester`
#: portion goes on the path explicitly, or the mixed-geometry note silently vanishes.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class GeoPythonError(RuntimeError):
    """Raised when the subprocess can't run or produces no verdict."""


def run_geo_python(
    code: str,
    *,
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Execute ``code`` with the geo stack bound and return the harness verdict.

    ``cwd`` is the working directory (Chester passes the GeoCache, so a bare output
    filename lands in the cache). Returns
    ``{"ok": bool, "result": ..., "stdout": str, "error": str|None,
    "outputs": [path], "calls": [{name, args, warning?}]}``.

    ``calls`` is the field that matters beyond the obvious: it reports the checked
    helpers the snippet used **inside the verdict's content**, where a validator
    reading tool returns can see them.

    Raises:
        GeoPythonError: on timeout, or if the harness wrote no verdict.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{_PACKAGE_ROOT}{os.pathsep}{existing}" if existing else str(_PACKAGE_ROOT)
    with tempfile.TemporaryDirectory(prefix="chester-geopy-") as td:
        code_path = Path(td) / "user_code.py"
        out_path = Path(td) / "verdict.json"
        code_path.write_text(code, encoding="utf-8")
        try:
            # No `cwd=`, `close_fds=False`: then CPython uses posix_spawn, not fork. A fork
            # of this threaded process hung before exec for 2 h 23 min (2026-10-04), out
            # of `timeout`'s reach. Our fds are non-inheritable (PEP 446); the harness chdirs.
            proc = subprocess.run(
                [sys.executable, str(_HARNESS), str(code_path), str(out_path),
                 *([cwd] if cwd else [])],
                env=env, capture_output=True, text=True, timeout=timeout,
                close_fds=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GeoPythonError(f"geo python code timed out after {timeout}s") from exc

        if not out_path.exists():
            detail = (proc.stderr or proc.stdout or "(no output)").strip()
            raise GeoPythonError(
                f"geo python harness produced no verdict (exit {proc.returncode}): "
                f"{detail[-800:]}"
            )
        return json.loads(out_path.read_text(encoding="utf-8"))


#: Was ein Schnipsel von Hand nachbaut → welche geprüfte Funktion es gäbe, und was
#: sie zusätzlich liefert. Jeder Eintrag ist ein Muster für die **rohe** Form; die
#: geprüfte Form wird daneben gesucht und schaltet den Eintrag ab.
#:
#: Gemessen 2026-09-06 (`buffer-schools-500m`, QGIS aus): Der Agent fand
#: `geo_python_run` sofort und schrieb darin dreimal rohes geopandas —
#: `gpd.read_file`, `to_crs`, `.buffer(500)`, `to_file`. Fachlich richtig, und jede
#: Zusicherung lief ins Leere: `outputs: []`, `calls: []`, **kein einziger
#: Provenienz-Sidecar**, keine Mixed-Geometry-Notiz. Der Notausgang war zur
#: Hauptstraße geworden — derselbe Befund, den `_search_first` für PyQGIS schon hat
#: (208 handgeschriebene Schnipsel gegen 70 Katalogsuchen).
_HAND_ROLLED: tuple[tuple[str, str, str, str], ...] = (
    (r"\.to_crs\s*\(", "vector_reproject", r"(?<![\w.])reproject\s*\(",
     "reports the feature count and the target CRS"),
    (r"\.buffer\s*\(", "vector_buffer", r"(?<![\w.])buffer\s*\(",
     "refuses a buffer in degrees instead of returning a plausibly wrong shape"),
    (r"gpd\.clip\s*\(|\.clip\s*\(", "vector_clip", r"(?<![\w.])clip\s*\(",
     "says so when a full input turns into an empty output"),
    (r"gpd\.overlay\s*\(", "vector_intersection", r"(?<![\w.])intersection\s*\(",
     "keeps both attribute sets and reports the feature counts"),
    (r"\.dissolve\s*\(", "vector_dissolve", r"(?<![\w.])dissolve\s*\(",
     "reports how many features are left"),
    # Nur wenn im selben Schnipsel eine Ebene gelesen wurde: `pd.concat` über zwei
    # reine Statistiktabellen ist völlig in Ordnung und hat kein geprüftes Gegenstück.
    # Gemessen 2026-09-07 (`buffer-schools-500m`): genau diese Form, und genau die
    # dabei entstandene Datei war die einzige des Laufs ohne Provenienz-Sidecar.
    (r"(?s)(?:gpd\.read_file|read_vector)[\s\S]*\bpd\.concat\s*\(", "vector_merge",
     r"(?<![\w.])merge\s*\(",
     "aligns the CRS instead of stopping at two of them and silently relabelling a "
     "layer that has none"),
    (r"rasterio\.features\.rasterize|features\.rasterize\s*\(", "rasterize",
     r"(?<![\w.])rasterize\s*\(", "refuses a resolution in degrees"),
    # `rasterstats` belongs here since 2026-09-27: it is the first thing a model reaches
    # for, and the raw pattern did not know it. Worse, its function is *also* called
    # `zonal_stats`, so the call matched the checked form and switched this entry off —
    # the guard stayed silent through `from rasterstats import zonal_stats` (package not
    # installed, snippet crashed) and through the `rasterio.mask(filled=True, nodata=0)`
    # that followed, which averaged the zeros outside each polygon into eighteen
    # district means. An import that shadows a bound operation is handled in
    # `hand_rolled_operations`, which is where the two spellings can be told apart.
    # **`rasterio.mask` alone is a clip, not a zonal statistic**, and Chester has no
    # tool for clipping a raster to a polygon — so the snippet is the right route and
    # there is nothing to redirect to. Measured 2026-09-27 (`terrain-ruggedness-index`):
    # the raster ressort masked the DEM to the city boundary, was told "a checked
    # function already does this: `zonal_stats`", and lost a round to a false alarm.
    # Like the `vector_merge` entry above, the ambiguous spelling therefore has to see
    # an **aggregation in the same snippet**; `zonal_statistics` and `rasterstats` name
    # the operation outright and need no second signal.
    (r"(?s)zonal_statistics|rasterstats|"
     r"(?:rasterio\.mask|rio_mask)[\s\S]*"
     r"(?:nan(?:mean|sum|median|max|min)|np\.(?:mean|sum|median|average)|"
     r"\.mean\s*\(|\.sum\s*\(|\bstatistics\b)", "zonal_stats",
     r"(?<![\w.])zonal_stats\s*\(",
     "masks nodata out and reports the coverage per zone"),
    (r"gpd\.read_file\s*\(", "read_vector", r"(?<![\w.])read_vector\s*\(",
     "collects the path spellings and warns about mixed geometry"),
    (r"\.to_file\s*\(", "write_vector", r"(?<![\w.])write_vector\s*\(",
     "puts the file in the GeoCache, **stamps the provenance** and reports it in `outputs`"),
)


def snippet_bound_names() -> frozenset[str]:
    """The names a snippet can call without importing anything.

    The checked operations lie in the snippet namespace next to the raw stack
    (`resources/geo_python_harness.py`), plus the two file helpers the harness itself
    provides. A guard that recommends an operation has to know this set, because
    ``read_vector`` and ``write_vector`` are **never** tools — recommending them as
    "call it directly, one call per step" was wrong for every agent, and for a ressort
    with a narrowed toolset the same is true of any operation outside its slice
    (measured 2026-09-27, `mean-elevation-per-district`: the vector ressort was sent to
    `zonal_stats`, which is the raster ressort's tool, three times).
    """
    names = {"read_vector", "write_vector"}
    for module in ("geoops", "rasterops", "terrainops", "networkops"):
        try:
            mod = __import__(f"chester.{module}", fromlist=["OPERATIONS"])
        except ImportError:  # pragma: no cover - the stack is a hard dependency
            continue
        names |= set(getattr(mod, "OPERATIONS", {}))
    return frozenset(names)


def hand_rolled_operations(code: str) -> list[tuple[str, str]]:
    """``(Funktionsname, was sie zusätzlich liefert)`` für jede nachgebaute Operation.

    Rein und ohne Kontext, damit sie sich ohne Agentenlauf prüfen lässt. Ein Eintrag
    zählt nur, wenn die **rohe** Form vorkommt und die geprüfte **nicht** — wer
    `clip(...)` ruft und daneben `gdf.clip(...)` schreibt, wird nicht angehalten.

    **An import cancels the checked form** (measured 2026-09-27): after
    `from rasterstats import zonal_stats` the call reads `zonal_stats(...)` — the same
    spelling as the bound operation, but not the same function and not the same argument
    order. The entry switched itself off. A shadowed name therefore counts as raw again.
    """
    import re

    found: list[tuple[str, str]] = []
    for raw, name, checked, gain in _HAND_ROLLED:
        shadowed = re.search(rf"import\s+[\w.]*\b{re.escape(name)}\b|"
                             rf"import\s+\w+\s+as\s+{re.escape(name)}\b", code or "")
        if re.search(raw, code or "") and (shadowed or not re.search(checked, code or "")):
            found.append((name, gain))
    return found
