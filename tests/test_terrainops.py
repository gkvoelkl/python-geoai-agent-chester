"""Terrain und Hydrologie ohne QGIS (`chester/terrainops.py`).

Phase KQ Schritt 3b. Die Entscheidung, welche Operation eine Abhaengigkeit braucht,
wurde gemessen statt nach Featureliste getroffen — und diese Tests halten die Messung
fest: Hangneigung gegen eine **analytisch bekannte** Flaeche, nicht gegen eine zweite
Implementierung.
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
    """Eine Ebene, die exakt TRUE_SLOPE_DEG nach Osten faellt — die Wahrheit ist bekannt."""
    _ws(tmp_path)
    # faellt nach OSTEN: hoch im Westen, niedrig im Osten — damit ist die erwartete
    # Exposition 90° und nicht ihr Gegenteil
    x = np.arange(n)[::-1] * res
    dem = np.tile(x * np.tan(np.radians(TRUE_SLOPE_DEG)), (n, 1)).astype("float32")
    path = tmp_path / "geocache" / f"{name}.tif"
    with rasterio.open(path, "w", driver="GTiff", height=n, width=n, count=1,
                       dtype="float32", crs=crs,
                       transform=from_origin(700000, 5400000 + n * res, res, res)) as dst:
        dst.write(dem, 1)
    return f"{name}.tif"


def test_slope_matches_the_analytic_truth(tmp_path):
    """Horn in sechs Zeilen numpy trifft die bekannte Neigung auf 1e-5 Grad.

    Das ist der Grund, warum `richdem`/`whitebox` fuer Hangneigung, Exposition,
    Schummerung und TRI **nicht** gebraucht werden: Sie kauften Bequemlichkeit, keine
    Richtigkeit. Gemessen 2026-09-06; GRASS' `r.slope.aspect` kam auf derselben
    Testflaeche auf dasselbe Maximum (57,12°).
    """
    dem = _tilted_plane(tmp_path)
    res = T.slope(dem, "slp.tif", workspace=_ws(tmp_path))
    assert res["ok"] is True and res["unit"] == "degrees"
    with rasterio.open(tmp_path / "geocache" / "slp.tif") as src:
        grid = src.read(1)
    inner = grid[2:-2, 2:-2]  # der Rand wird per `edge`-Padding extrapoliert
    assert abs(inner.mean() - TRUE_SLOPE_DEG) < 1e-4
    assert abs(inner - TRUE_SLOPE_DEG).max() < 1e-3


def test_aspect_points_the_right_way(tmp_path):
    """Eine nach Osten abfallende Ebene schaut nach Osten — 90°."""
    dem = _tilted_plane(tmp_path)
    T.aspect(dem, "asp.tif", workspace=_ws(tmp_path))
    with rasterio.open(tmp_path / "geocache" / "asp.tif") as src:
        grid = src.read(1)[2:-2, 2:-2]
    assert abs(grid.mean() - 90.0) < 1.0, f"Exposition {grid.mean():.1f}° statt 90°"


def test_ruggedness_is_zero_on_a_plane(tmp_path):
    """Eine Ebene ist nicht rau — auch wenn sie steil ist.

    Der Unterschied zur Hangneigung: TRI misst die *Unregelmaessigkeit*, nicht die
    Neigung. Eine gleichmaessige 30°-Flanke hat einen konstanten Hoehenunterschied
    zwischen Nachbarn und damit eine konstante, kleine Rauheit.
    """
    dem = _tilted_plane(tmp_path)
    T.ruggedness(dem, "tri.tif", workspace=_ws(tmp_path))
    with rasterio.open(tmp_path / "geocache" / "tri.tif") as src:
        grid = src.read(1)[2:-2, 2:-2]
    assert grid.std() < 1e-3, "eine gleichmaessige Flanke hat konstante Rauheit"


def test_hillshade_says_it_is_a_picture(tmp_path):
    """Eine Schummerung sieht aus wie Gelaendedaten und traegt keine."""
    dem = _tilted_plane(tmp_path)
    res = T.hillshade(dem, "hs.tif", workspace=_ws(tmp_path))
    assert res["ok"] is True
    assert "not a measurement" in res["note"]
    assert 0 <= res["range"][0] and res["range"][1] <= 255


def test_hydrology_says_what_is_missing_when_grass_is_absent(tmp_path, monkeypatch):
    """Ohne GRASS eine ehrliche Absage, kein obskurer Fehler.

    Senken fuellen und Abfluss akkumulieren sind ein Priority-Flood-Problem, kein
    Fensteroperator — dafuer gibt es keinen numpy-Einzeiler, und so etwas zu
    behaupten waere schlimmer als die Absage.
    """
    monkeypatch.setattr(T, "_grass_bin", lambda: None)
    dem = _tilted_plane(tmp_path)
    res = T.fill_sinks(dem, "filled.tif", workspace=_ws(tmp_path))
    assert res["ok"] is False
    assert "GRASS is not installed" in res["error"]
    assert "Slope, aspect, hillshade and ruggedness need none of that" in res["error"]


@pytest.mark.skipif(not T.grass_available(), reason="GRASS nicht installiert")
def test_flow_accumulation_runs_through_grass(tmp_path):
    """Der Subprozess-Weg ueber `grass -c … --exec`, gemessen bei 0,4 s.

    `import grass.script` in Chesters Interpreter scheitert mit „No active GRASS
    session" — die Module brauchen eine Umgebung, die nur der Launcher setzt.
    """
    dem = _tilted_plane(tmp_path)
    res = T.flow_accumulation(dem, "acc.tif", workspace=_ws(tmp_path))
    assert res["ok"] is True, res.get("error")
    assert res["algorithm"] == "grass:r.watershed"
    assert "upslope cells, not water volume" in res["note"]
    assert (tmp_path / "geocache" / "acc.tif").is_file()
