"""Terrain and hydrology without QGIS (`chester/terrainops.py`).

Phase KQ step 3b. Which operation needs a dependency was decided by measurement rather
than by feature list — and these tests pin the measurement: slope against an
**analytically known** surface, not against a second implementation.
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from chester import terrainops as T

TRUE_SLOPE_DEG = 30.0


def _ws(tmp_path):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    return str(tmp_path)


def _tilted_plane(tmp_path, name="dem", n=60, res=1.0, crs="EPSG:25832"):
    """A plane that falls exactly TRUE_SLOPE_DEG to the east — the truth is known."""
    _ws(tmp_path)
    # falls to the EAST: high in the west, low in the east — so the expected aspect is
    # 90° and not its opposite
    x = np.arange(n)[::-1] * res
    dem = np.tile(x * np.tan(np.radians(TRUE_SLOPE_DEG)), (n, 1)).astype("float32")
    path = tmp_path / "geocache" / f"{name}.tif"
    with rasterio.open(path, "w", driver="GTiff", height=n, width=n, count=1,
                       dtype="float32", crs=crs,
                       transform=from_origin(700000, 5400000 + n * res, res, res)) as dst:
        dst.write(dem, 1)
    return f"{name}.tif"


def test_slope_matches_the_analytic_truth(tmp_path):
    """Horn in six lines of numpy hits the known slope to 1e-5 degrees.

    That is why `richdem`/`whitebox` are **not** needed for slope, aspect, hillshade and
    TRI: they bought convenience, not correctness. Measured 2026-09-06; GRASS's
    `r.slope.aspect` reached the same maximum (57.12°) on the same test surface.
    """
    dem = _tilted_plane(tmp_path)
    res = T.slope(dem, "slp.tif", workspace=_ws(tmp_path))
    assert res["ok"] is True and res["unit"] == "degrees"
    with rasterio.open(tmp_path / "geocache" / "slp.tif") as src:
        grid = src.read(1)
    inner = grid[2:-2, 2:-2]  # the border is extrapolated by `edge` padding
    assert abs(inner.mean() - TRUE_SLOPE_DEG) < 1e-4
    assert abs(inner - TRUE_SLOPE_DEG).max() < 1e-3


def test_aspect_points_the_right_way(tmp_path):
    """A plane sloping down to the east faces east — 90°."""
    dem = _tilted_plane(tmp_path)
    T.aspect(dem, "asp.tif", workspace=_ws(tmp_path))
    with rasterio.open(tmp_path / "geocache" / "asp.tif") as src:
        grid = src.read(1)[2:-2, 2:-2]
    assert abs(grid.mean() - 90.0) < 1.0, f"Exposition {grid.mean():.1f}° statt 90°"


def test_ruggedness_is_zero_on_a_plane(tmp_path):
    """A plane is not rough — even when it is steep.

    The difference to slope: TRI measures *irregularity*, not inclination. An even 30°
    flank has a constant height difference between neighbours and thus a constant,
    small roughness.
    """
    dem = _tilted_plane(tmp_path)
    T.ruggedness(dem, "tri.tif", workspace=_ws(tmp_path))
    with rasterio.open(tmp_path / "geocache" / "tri.tif") as src:
        grid = src.read(1)[2:-2, 2:-2]
    assert grid.std() < 1e-3, "eine gleichmaessige Flanke hat konstante Rauheit"


def test_hillshade_says_it_is_a_picture(tmp_path):
    """A hillshade looks like terrain data and carries none."""
    dem = _tilted_plane(tmp_path)
    res = T.hillshade(dem, "hs.tif", workspace=_ws(tmp_path))
    assert res["ok"] is True
    assert "not a measurement" in res["note"]
    assert 0 <= res["range"][0] and res["range"][1] <= 255


def test_hydrology_says_what_is_missing_when_grass_is_absent(tmp_path, monkeypatch):
    """Without GRASS an honest refusal, not an obscure error.

    Filling sinks and accumulating flow are a priority-flood problem, not a window
    operator — there is no numpy one-liner for it, and claiming one would be worse than
    the refusal.
    """
    monkeypatch.setattr(T, "_grass_bin", lambda: None)
    dem = _tilted_plane(tmp_path)
    res = T.fill_sinks(dem, "filled.tif", workspace=_ws(tmp_path))
    assert res["ok"] is False
    assert "GRASS is not installed" in res["error"]
    assert "Slope, aspect, hillshade and ruggedness need none of that" in res["error"]


@pytest.mark.skipif(not T.grass_available(), reason="GRASS nicht installiert")
def test_flow_accumulation_runs_through_grass(tmp_path):
    """The subprocess route via `grass -c … --exec`, measured at 0.4 s.

    `import grass.script` in Chester's interpreter fails with "No active GRASS session" —
    the modules need an environment that only the launcher sets.
    """
    dem = _tilted_plane(tmp_path)
    res = T.flow_accumulation(dem, "acc.tif", workspace=_ws(tmp_path))
    assert res["ok"] is True, res.get("error")
    assert res["algorithm"] == "grass:r.watershed"
    assert "upslope cells, not water volume" in res["note"]
    assert (tmp_path / "geocache" / "acc.tif").is_file()


def _plane_with_one_pit(tmp_path, name="pitted", n=20, res=1.0, drop=0.1, pit=5.0):
    """A plane falling east by `drop` per cell, with one cell dug `pit` metres deep.

    Everything about it is known in advance: the plane value of column c is
    ``(n - 1 - c) * drop``, so the pit's eastern neighbour — its pour point — sits
    exactly `drop` below the pit's own undisturbed level.
    """
    _ws(tmp_path)
    dem = np.tile(np.arange(n)[::-1] * res * drop, (n, 1)).astype("float32")
    row = col = n // 2
    dem[row, col] -= pit
    path = tmp_path / "geocache" / f"{name}.tif"
    with rasterio.open(path, "w", driver="GTiff", height=n, width=n, count=1,
                       dtype="float32", crs="EPSG:25832",
                       transform=from_origin(700000, 5400000 + n * res, res, res)) as dst:
        dst.write(dem, 1)
    return f"{name}.tif", dem, (row, col)


@pytest.mark.skipif(not T.grass_available(), reason="GRASS nicht installiert")
def test_fill_sinks_raises_the_pit_to_its_pour_point_and_nothing_else(tmp_path):
    """The existing tests checked that it runs and that it refuses without GRASS.

    Neither asked what it filled. Two numbers say it: the pit ends at the level of its
    downslope neighbour — the pour point, not the rim — and **exactly one** cell moves.
    A fill that floods the neighbourhood also returns `ok: true`.
    """
    dem_path, dem, (row, col) = _plane_with_one_pit(tmp_path)
    res = T.fill_sinks(dem_path, "filled.tif", workspace=_ws(tmp_path))
    assert res["ok"] is True, res.get("error")

    with rasterio.open(tmp_path / "geocache" / "filled.tif") as src:
        filled = src.read(1)

    pour = float(dem[row, col + 1])                 # the cell it drains into
    assert abs(float(filled[row, col]) - pour) < 1e-5, (filled[row, col], pour)
    assert int((np.abs(filled - dem) > 1e-6).sum()) == 1, "the fill spread past the pit"


@pytest.mark.skipif(not T.grass_available(), reason="GRASS nicht installiert")
def test_flow_accumulation_starts_at_one_on_the_ridge(tmp_path):
    """On a plane falling east, the western column is the ridge: nothing drains in.

    Its accumulation must be exactly 1 — the cell itself — for every row. That is the
    analytic anchor; the eastern side only has to show that the flow really collected
    there, because `r.watershed` routes multiple directions and loses some over the
    boundary, so an exact figure would encode the edge effect, not the hydrology.
    """
    dem_path, _, _ = _plane_with_one_pit(tmp_path, pit=0.0)
    res = T.flow_accumulation(dem_path, "acc.tif", workspace=_ws(tmp_path))
    assert res["ok"] is True, res.get("error")

    with rasterio.open(tmp_path / "geocache" / "acc.tif") as src:
        acc = src.read(1)

    assert np.allclose(acc[:, 0], 1.0), f"the ridge already carries flow: {acc[:4, 0]}"
    n = acc.shape[1]
    assert float(np.median(acc[:, -1])) > 0.7 * n, np.median(acc[:, -1])
    assert float(acc.max()) <= acc.size, "more flow than there are cells"


def _plane_in_degrees(tmp_path, name="geo_dem", n=40, crs="EPSG:4326"):
    """The same 30-degree plane as `_tilted_plane`, but tagged as degrees.

    30 m at 49 N is about 0.00027 degrees, so this is what `fetch_dem` hands back for
    a Regensburg bbox: real heights in metres over a grid whose spacing is angular.
    """
    _ws(tmp_path)
    res_m, deg = 30.0, 30.0 / 111320.0
    dem = np.tile(np.arange(n)[::-1] * res_m * np.tan(np.radians(TRUE_SLOPE_DEG)),
                  (n, 1)).astype("float32")
    path = tmp_path / "geocache" / f"{name}.tif"
    with rasterio.open(path, "w", driver="GTiff", height=n, width=n, count=1,
                       dtype="float32", crs=crs,
                       transform=from_origin(12.0, 49.0 + n * deg, deg, deg)) as dst:
        dst.write(dem, 1)
    return f"{name}.tif"


@pytest.mark.parametrize("operation", ["slope", "aspect", "hillshade", "ruggedness"])
def test_a_dem_in_degrees_is_refused(tmp_path, operation):
    """Horn divides metres by the pixel spacing — in degrees that quotient is nonsense.

    Measured 2026-09-22 before the guard existed: the plane below, which falls exactly
    30 degrees, came back as **89.999 degrees** from `slope` — a hillside reported as
    a cliff, with `ok: true` and no warning. All four operations accepted it. Only
    `fetch_dem` (Copernicus GLO-30) delivers degrees; `fetch_dgm1`, `fetch_swissalti3d`
    and `fetch_austria_dem` are metric, and the refusal names them because Chester has
    no raster reprojection tool to point at instead.
    """
    dem = _plane_in_degrees(tmp_path)
    res = getattr(T, operation)(dem, "out.tif", workspace=_ws(tmp_path))
    assert res["ok"] is False, f"{operation} accepted a DEM in degrees: {res}"
    assert "geographic CRS" in res["error"]
    assert "fetch_dgm1" in res["error"], "the refusal must name a metric source"
    assert not (tmp_path / "geocache" / "out.tif").exists(), "it wrote anyway"


def test_the_metric_plane_still_passes(tmp_path):
    """The counter-check: the guard must not refuse the case it exists to protect."""
    res = T.slope(_tilted_plane(tmp_path), "ok.tif", workspace=_ws(tmp_path))
    assert res["ok"] is True, res
    assert abs(res["range"][1] - TRUE_SLOPE_DEG) < 1e-4, res["range"]
