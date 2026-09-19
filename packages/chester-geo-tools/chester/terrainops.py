"""Terrain and hydrology without QGIS — numpy where it suffices, GRASS where it does not.

Phase KQ step 3b (`internal/TODO.md`), and the decision was measured rather than argued:

* **Slope, aspect, hillshade, ruggedness need no dependency at all.** Horn's 3×3
  operator is six lines of numpy, and against an analytically known surface (a plane
  tilted exactly 30°) it lands within **1e-5°**. `richdem`/`whitebox` would buy
  convenience, not correctness.
* **Hydrology does.** Filling sinks and accumulating flow is a priority-flood problem,
  not a window operator. GRASS does it, is already a documented part of this
  installation, and — measured 2026-09-06 — creates a project, imports the DEM, runs
  `r.slope.aspect` *and* `r.watershed` in **0.49 s** wall clock.

The GRASS binding is a **subprocess**, the same shape as `qgis_process`, and for the
same reason: `import grass.script` into Chester's interpreter fails with "No active
GRASS session" — the modules need an environment that only the `grass` binary sets up.
`grass -c epsg:<code> <project> --exec python <script>` is the supported entry point.

GRASS stays **optional**: without it the numpy operations work and the hydrology ones
say what is missing instead of failing obscurely.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

from chester import provenance
from chester.workspace import DEFAULT_WORKSPACE, resolve_path


def _grass_bin() -> Path | None:
    """The `grass` launcher of the installation `qgis_env` already knows how to find."""
    from chester.qgis_env import find_gisbase

    gisbase = find_gisbase()
    if gisbase is None:
        return None
    candidate = Path(gisbase) / "bin" / "grass"
    return candidate if candidate.exists() else None


def grass_available() -> bool:
    """Whether the hydrology operations can run at all."""
    return _grass_bin() is not None


def _read(path: str, ws: str):
    import numpy as np
    import rasterio

    with rasterio.open(resolve_path(path, ws)) as src:
        band = src.read(1).astype("float64")
        if src.nodata is not None:
            band = np.where(band == src.nodata, np.nan, band)
        return band, src.profile, src.res


def _write(grid, profile, output_path: str, ws: str, tool: str, query: str | None = None) -> str:
    import numpy as np
    import rasterio

    out = resolve_path(output_path, ws, write=True)
    profile = dict(profile)
    profile.update(dtype="float32", count=1, nodata=np.nan)
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(np.asarray(grid, dtype="float32"), 1)
    provenance.write_meta(out, source="chester", tool=tool, query=query)
    return out


def _gradients(z, xres: float, yres: float):
    """Horn's 3×3 operator — what GRASS and GDAL use, so results stay comparable."""
    import numpy as np

    p = np.pad(z, 1, mode="edge")
    dzdx = ((p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:])
            - (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])) / (8 * xres)
    dzdy = ((p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:])
            - (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])) / (8 * yres)
    return dzdx, dzdy


def _facts(grid, output: str, **extra) -> dict:
    import numpy as np

    finite = grid[np.isfinite(grid)]
    facts: dict[str, Any] = {"ok": True, "output": output, **extra}
    facts["range"] = [float(finite.min()), float(finite.max())] if finite.size else None
    if not finite.size:
        facts["warning"] = ("every cell came out nodata — the input DEM is empty or "
                            "entirely nodata. Do not report this as a result.")
    return facts


def slope(dem_path: str, output_path: str, *, workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Slope in **degrees** from a DEM whose units are metres."""
    import numpy as np

    z, profile, res = _read(dem_path, workspace)
    dzdx, dzdy = _gradients(z, res[0], res[1])
    grid = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    return _facts(grid, _write(grid, profile, output_path, workspace, "slope"), unit="degrees")


def aspect(dem_path: str, output_path: str, *, workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Aspect in degrees clockwise from north (0 = N, 90 = E)."""
    import numpy as np

    z, profile, res = _read(dem_path, workspace)
    dzdx, dzdy = _gradients(z, res[0], res[1])
    # Exposition ist die Himmelsrichtung des **Gefälles**, nicht des Anstiegs.
    # `dzdy` zählt hier pro Zeile nach Süden (Zeilenindex wächst südwärts), ist also
    # bereits -dz/dNord; die Ostkomponente des Gefälles ist -dzdx, die Nordkomponente
    # +dzdy. Azimut = atan2(Ost, Nord), 0° = N, 90° = O. Eine erste Fassung nahm
    # atan2(dzdy, -dzdx) und lag um 90° daneben — der Test gegen eine Fläche mit
    # bekannter Neigungsrichtung hat es gefunden (2026-09-06).
    grid = (np.degrees(np.arctan2(-dzdx, dzdy)) + 360.0) % 360.0
    return _facts(grid, _write(grid, profile, output_path, workspace, "aspect"), unit="degrees")


def hillshade(dem_path: str, output_path: str, *, azimuth: float = 315.0,
              altitude: float = 45.0, workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Shaded relief, 0–255 — a picture, never a measurement.

    Say so when you report it: a hillshade looks like terrain data and carries none.
    """
    import numpy as np

    z, profile, res = _read(dem_path, workspace)
    dzdx, dzdy = _gradients(z, res[0], res[1])
    slope_rad = np.arctan(np.hypot(dzdx, dzdy))
    # Dieselbe Kompasskonvention wie `aspect` (0° = N, im Uhrzeigersinn), damit
    # Beleuchtungsrichtung und Hangausrichtung ohne Umrechnung vergleichbar sind.
    aspect_rad = np.arctan2(-dzdx, dzdy)
    zen = np.radians(90.0 - altitude)
    az = np.radians(azimuth)
    grid = 255.0 * ((np.cos(zen) * np.cos(slope_rad))
                    + (np.sin(zen) * np.sin(slope_rad) * np.cos(az - aspect_rad)))
    grid = np.clip(grid, 0, 255)
    facts = _facts(grid, _write(grid, profile, output_path, workspace, "hillshade"))
    facts["note"] = ("a hillshade is a rendering, not a measurement — never read "
                     "heights or slopes off it.")
    return facts


def ruggedness(dem_path: str, output_path: str, *,
               workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Terrain Ruggedness Index (Riley): mean absolute height difference to the 8 neighbours."""
    import numpy as np

    z, profile, _res = _read(dem_path, workspace)
    p = np.pad(z, 1, mode="edge")
    diffs = [np.abs(p[1:-1, 1:-1] - p[1 + dy:p.shape[0] - 1 + dy, 1 + dx:p.shape[1] - 1 + dx])
             for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0)]
    grid = np.mean(diffs, axis=0)
    return _facts(grid, _write(grid, profile, output_path, workspace, "ruggedness"),
                  unit="metres")


_GRASS_MISSING = (
    "GRASS is not installed, and filling sinks / accumulating flow is a "
    "priority-flood problem, not a window operator — there is no honest numpy "
    "one-liner for it. Install GRASS (a separate download; Chester finds it by "
    "itself or via CHESTER_GRASS_APP) and this works. Slope, aspect, hillshade and "
    "ruggedness need none of that and are available now."
)


def _run_grass(script: str, epsg: str, timeout: int = 900) -> tuple[bool, str]:
    """Run a GRASS python job in a throwaway project. Returns ``(ok, output)``.

    A fresh project per call: hydrology jobs are one-shot here, and a persistent
    mapset would be state nobody reconciles. `grass -c … --exec` is the documented
    entry point — importing `grass.script` into this interpreter fails with "No
    active GRASS session", because the modules need an environment only the launcher
    sets up (measured 2026-09-06).
    """
    binary = _grass_bin()
    if binary is None:
        return False, _GRASS_MISSING
    with tempfile.TemporaryDirectory(prefix="chester-grass-") as td:
        job = Path(td) / "job.py"
        job.write_text(script, encoding="utf-8")
        try:
            proc = subprocess.run(
                [str(binary), "-c", f"epsg:{epsg}", str(Path(td) / "project"),
                 "--exec", "python", str(job)],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"GRASS timed out after {timeout}s"
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "(no output)").strip()[-800:]
        return True, proc.stdout


def _epsg_of(path: str, ws: str) -> str | None:
    import rasterio

    with rasterio.open(resolve_path(path, ws)) as src:
        code = src.crs.to_epsg() if src.crs else None
    return str(code) if code else None


def fill_sinks(dem_path: str, output_path: str, *,
               workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Fill depressions so water can leave every cell (GRASS ``r.fill.dir``).

    Do this **before** accumulating flow. An unfilled sink swallows the flow that
    should have continued downstream, and the accumulation raster then shows a
    drainage network that stops in the middle of a field.
    """
    epsg = _epsg_of(dem_path, workspace)
    if epsg is None:
        return {"ok": False, "error": "the DEM has no EPSG code — GRASS needs one"}
    src = resolve_path(dem_path, workspace)
    out = resolve_path(output_path, workspace, write=True)
    ok, detail = _run_grass(
        "import grass.script as gs\n"
        f"gs.run_command('r.in.gdal', input={src!r}, output='dem', overwrite=True, quiet=True)\n"
        "gs.run_command('g.region', raster='dem', quiet=True)\n"
        "gs.run_command('r.fill.dir', input='dem', output='filled', direction='dir',"
        " overwrite=True, quiet=True)\n"
        f"gs.run_command('r.out.gdal', input='filled', output={out!r}, format='GTiff',"
        " overwrite=True, quiet=True)\n",
        epsg,
    )
    if not ok:
        return {"ok": False, "error": detail}
    provenance.write_meta(out, source="chester", tool="fill_sinks", query="r.fill.dir")
    return {"ok": True, "output": out, "algorithm": "grass:r.fill.dir"}


def flow_accumulation(dem_path: str, output_path: str, *,
                      workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Accumulated flow per cell (GRASS ``r.watershed``, ``-a`` for absolute values).

    Feed it a **filled** DEM. The values count upslope cells, not litres; a cell with
    50.000 is a valley floor, not a flood forecast.
    """
    epsg = _epsg_of(dem_path, workspace)
    if epsg is None:
        return {"ok": False, "error": "the DEM has no EPSG code — GRASS needs one"}
    src = resolve_path(dem_path, workspace)
    out = resolve_path(output_path, workspace, write=True)
    ok, detail = _run_grass(
        "import grass.script as gs\n"
        f"gs.run_command('r.in.gdal', input={src!r}, output='dem', overwrite=True, quiet=True)\n"
        "gs.run_command('g.region', raster='dem', quiet=True)\n"
        "gs.run_command('r.watershed', elevation='dem', accumulation='acc', flags='a',"
        " overwrite=True, quiet=True)\n"
        f"gs.run_command('r.out.gdal', input='acc', output={out!r}, format='GTiff',"
        " overwrite=True, quiet=True)\n",
        epsg,
    )
    if not ok:
        return {"ok": False, "error": detail}
    provenance.write_meta(out, source="chester", tool="flow_accumulation",
                          query="r.watershed -a")
    return {"ok": True, "output": out, "algorithm": "grass:r.watershed",
            "note": ("values count upslope cells, not water volume. Feed a filled DEM "
                     "or the network stops at the first depression.")}


#: The six, by the name the snippet namespace uses — like `geoops`/`rasterops`.
OPERATIONS = {
    "slope": slope,
    "aspect": aspect,
    "hillshade": hillshade,
    "ruggedness": ruggedness,
    "fill_sinks": fill_sinks,
    "flow_accumulation": flow_accumulation,
}
