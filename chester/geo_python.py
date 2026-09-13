"""Run a GeoPandas snippet in a subprocess of Chester's own interpreter.

The sibling of :mod:`chester.qgis_python`. That one exists because PyQGIS must never
be imported into this venv, so the snippet has to run in QGIS's interpreter; here the
libraries are already present and the subprocess is a deliberate choice, not a
necessity:

* the timeout is enforceable (a runaway snippet is killed, not waited on),
* a segfault in GDAL/GEOS ends the child, not the agent,
* the snippet cannot mutate Chester's process state — no stray ``sys.path`` entry, no
  matplotlib backend switched under the map renderer, no ``os.chdir``.

Phase KQ step 1 (`internal/TODO.md`): the mechanism that made `qgis_python` work,
pointed at a namespace that needs no QGIS installation.
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
#: The harness imports `chester.geofacts` for the mixed-geometry note. The snippet's
#: CWD is the GeoCache, so the repository root has to be on the path explicitly —
#: without it the import fails silently and the note is simply absent.
_REPO_ROOT = Path(__file__).resolve().parent.parent


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
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
    with tempfile.TemporaryDirectory(prefix="chester-geopy-") as td:
        code_path = Path(td) / "user_code.py"
        out_path = Path(td) / "verdict.json"
        code_path.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, str(_HARNESS), str(code_path), str(out_path)],
                env=env,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
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
     "meldet Objektzahl und Ziel-CRS zurück"),
    (r"\.buffer\s*\(", "vector_buffer", r"(?<![\w.])buffer\s*\(",
     "lehnt einen Puffer in Grad ab, statt eine plausibel falsche Form zu liefern"),
    (r"gpd\.clip\s*\(|\.clip\s*\(", "vector_clip", r"(?<![\w.])clip\s*\(",
     "sagt es, wenn aus voller Eingabe eine leere Ausgabe wird"),
    (r"gpd\.overlay\s*\(", "vector_intersection", r"(?<![\w.])intersection\s*\(",
     "hält beide Attributsätze und meldet die Objektzahlen"),
    (r"\.dissolve\s*\(", "vector_dissolve", r"(?<![\w.])dissolve\s*\(",
     "meldet, wie viele Objekte übrig bleiben"),
    # Nur wenn im selben Schnipsel eine Ebene gelesen wurde: `pd.concat` über zwei
    # reine Statistiktabellen ist völlig in Ordnung und hat kein geprüftes Gegenstück.
    # Gemessen 2026-09-07 (`buffer-schools-500m`): genau diese Form, und genau die
    # dabei entstandene Datei war die einzige des Laufs ohne Provenienz-Sidecar.
    (r"(?s)(?:gpd\.read_file|read_vector)[\s\S]*\bpd\.concat\s*\(", "vector_merge",
     r"(?<![\w.])merge\s*\(",
     "gleicht die CRS an, statt bei zweien abzubrechen und eine Ebene ohne CRS still "
     "umzuetikettieren"),
    (r"rasterio\.features\.rasterize|features\.rasterize\s*\(", "rasterize",
     r"(?<![\w.])rasterize\s*\(", "lehnt eine Auflösung in Grad ab"),
    (r"rasterio\.mask|rio_mask|zonal_statistics", "zonal_stats",
     r"(?<![\w.])zonal_stats\s*\(",
     "maskiert nodata heraus und meldet die Abdeckung je Zone"),
    (r"gpd\.read_file\s*\(", "read_vector", r"(?<![\w.])read_vector\s*\(",
     "sammelt die Pfadschreibweisen ein und warnt vor gemischter Geometrie"),
    (r"\.to_file\s*\(", "write_vector", r"(?<![\w.])write_vector\s*\(",
     "legt die Datei im GeoCache ab, **stempelt die Provenienz** und meldet sie in `outputs`"),
)


def hand_rolled_operations(code: str) -> list[tuple[str, str]]:
    """``(Funktionsname, was sie zusätzlich liefert)`` für jede nachgebaute Operation.

    Rein und ohne Kontext, damit sie sich ohne Agentenlauf prüfen lässt. Ein Eintrag
    zählt nur, wenn die **rohe** Form vorkommt und die geprüfte **nicht** — wer
    `clip(...)` ruft und daneben `gdf.clip(...)` schreibt, wird nicht angehalten.
    """
    import re

    found: list[tuple[str, str]] = []
    for raw, name, checked, gain in _HAND_ROLLED:
        if re.search(raw, code or "") and not re.search(checked, code or ""):
            found.append((name, gain))
    return found
