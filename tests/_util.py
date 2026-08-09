"""Shared test helpers: tool extraction, QGIS gating, and synthetic data."""

from __future__ import annotations

from pathlib import Path

import pytest

from chester.qgis_env import QgisNotFoundError, resolve_qgis_env


def qgis_available() -> bool:
    try:
        resolve_qgis_env()
        return True
    except QgisNotFoundError:
        return False


# QGIS integration tests skip (not fail) when no local qgis_process is present.
requires_qgis = pytest.mark.skipif(
    not qgis_available(), reason="qgis_process not available"
)


def tools_of(capability) -> dict:
    """Return {tool_name: callable} for a capability's FunctionToolset."""
    toolset = capability.get_toolset()
    out = {}
    for name, tool in toolset.tools.items():
        fn = getattr(tool, "function", None) or getattr(tool, "func", None) or tool
        out[name] = fn
    return out


# ── synthetic data builders ─────────────────────────────────────────────────


def write_point(path: Path, x: float, y: float, crs: str) -> Path:
    import geopandas as gpd
    from shapely.geometry import Point

    gpd.GeoDataFrame({"name": ["p"]}, geometry=[Point(x, y)], crs=crs).to_file(path)
    return path


def write_bands(green_path: Path, nir_path: Path) -> tuple[Path, Path]:
    """Two 60x60 bands (EPSG:25832) with a 20x20 high-NDWI water block (area 400 m2)."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    prof = dict(
        driver="GTiff", dtype="float32", count=1, width=60, height=60,
        crs="EPSG:25832", transform=from_origin(500000, 5600060, 1, 1),
    )
    green = np.full((60, 60), 0.20, dtype="float32")
    nir = np.full((60, 60), 0.40, dtype="float32")
    green[20:40, 20:40] = 0.30  # water block
    nir[20:40, 20:40] = 0.10
    for path, arr in ((green_path, green), (nir_path, nir)):
        with rasterio.open(path, "w", **prof) as ds:
            ds.write(arr, 1)
    return green_path, nir_path


def write_slope_dtm(path: Path) -> Path:
    """A 40x40 DTM that rises 1 m per 1 m eastward → true slope 45°."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    z = np.tile(np.arange(40, dtype="float32"), (40, 1))  # elevation == x
    prof = dict(
        driver="GTiff", dtype="float32", count=1, width=40, height=40,
        crs="EPSG:25832", transform=from_origin(500000, 5600040, 1, 1),
    )
    with rasterio.open(path, "w", **prof) as ds:
        ds.write(z, 1)
    return path


def write_building_sample(out_dir: Path) -> dict:
    """Buildings (heights 8/18/25 m) + DSM/DTM so DSM−DTM recovers heights.

    Returns the paths and the set of names taller than 15 m.
    """
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.features import rasterize
    from rasterio.transform import from_origin
    from shapely.geometry import box

    out_dir.mkdir(parents=True, exist_ok=True)
    buildings = [
        ("Klein", box(500000, 5600000, 500020, 5600020), 8.0),
        ("Mittel", box(500060, 5600040, 500084, 5600064), 18.0),
        ("Hoch", box(499960, 5600060, 499990, 5600080), 25.0),
    ]
    bpath = out_dir / "buildings.geojson"
    gpd.GeoDataFrame(
        {"name": [b[0] for b in buildings], "true_height": [b[2] for b in buildings]},
        geometry=[b[1] for b in buildings], crs="EPSG:25832",
    ).to_file(bpath)

    minx, miny, maxx, maxy = 499940, 5599980, 500120, 5600120
    w, h = int(maxx - minx), int(maxy - miny)
    transform = from_origin(minx, maxy, 1, 1)
    dtm = np.full((h, w), 50.0, dtype="float32")
    above = rasterize(
        [(g, ht) for _, g, ht in buildings],
        out_shape=(h, w), transform=transform, fill=0.0, dtype="float32",
    )
    prof = dict(
        driver="GTiff", dtype="float32", count=1, width=w, height=h,
        crs="EPSG:25832", transform=transform, nodata=-9999.0,
    )
    dtm_path, dsm_path = out_dir / "dtm.tif", out_dir / "dsm.tif"
    for path, arr in ((dtm_path, dtm), (dsm_path, dtm + above)):
        with rasterio.open(path, "w", **prof) as ds:
            ds.write(arr, 1)

    return {
        "buildings": bpath, "dtm": dtm_path, "dsm": dsm_path,
        "tall_names": {"Mittel", "Hoch"},
    }
