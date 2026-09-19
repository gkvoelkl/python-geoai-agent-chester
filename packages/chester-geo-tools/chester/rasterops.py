"""The four raster operations, on rasterio — pure, no agent stack, no QGIS.

Phase KQ step 3 (`internal/TODO.md`). The sibling of :mod:`chester.geoops`: same
contract (paths in, facts out), same path resolution, same provenance stamping, and
the same rule that a result which did nothing has to say so.

`rasterstats` is deliberately **not** a dependency. Its zonal statistics are twenty
lines of `rasterio.mask` and numpy, and the whole point of this phase is that Chester
installs with pip and nothing else; a package whose own dependencies are rasterio,
fiona and shapely buys convenience, not capability.

Two traps are encoded, both from this project's own runs:

* **nodata is not a value.** A DEM's -9999 fill averaged into a mean is the classic
  silently-wrong number. Every statistic here masks it out and reports how much of the
  zone was actually covered.
* **A zone that covers nothing** gets `null`, never 0 — the difference between "no
  supermarkets here" and "the raster does not reach this place".
"""

from __future__ import annotations

from typing import Any

from chester import provenance
from chester.workspace import DEFAULT_WORKSPACE, resolve_path


def _open(path: str, ws: str):
    import rasterio

    return rasterio.open(resolve_path(path, ws))


def _stamp(path: str, tool: str, query: str | None = None) -> str:
    provenance.write_meta(path, source="chester", tool=tool, query=query)
    return path


def rasterize(vector_path: str, output_path: str, *, resolution: float,
              column: str | None = None, burn: float = 1.0,
              workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Burn a vector layer into a new raster grid of ``resolution`` map units.

    ``column`` burns that attribute's value per feature; without it every feature
    burns ``burn``. The grid is derived from the layer's own extent and CRS — no
    reprojection happens silently.
    """
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.features import rasterize as _rasterize
    from rasterio.transform import from_origin

    gdf = gpd.read_file(resolve_path(vector_path, ws := workspace))
    if gdf.empty:
        return {"ok": False, "error": f"{vector_path} holds no features — nothing to burn"}
    if gdf.crs is not None and gdf.crs.is_geographic:
        return {"ok": False, "error": (
            f"{gdf.crs} is a geographic CRS — a resolution of {resolution} would be "
            "DEGREES, not metres. Reproject to a metric CRS first.")}
    minx, miny, maxx, maxy = gdf.total_bounds
    width = max(1, int((maxx - minx) / resolution))
    height = max(1, int((maxy - miny) / resolution))
    shapes = ((geom, float(val)) for geom, val in zip(
        gdf.geometry, gdf[column] if column else [burn] * len(gdf), strict=False))
    transform = from_origin(minx, maxy, resolution, resolution)
    grid = _rasterize(shapes, out_shape=(height, width), transform=transform,
                      fill=np.nan, dtype="float32")
    out = resolve_path(output_path, ws, write=True)
    with rasterio.open(out, "w", driver="GTiff", height=height, width=width, count=1,
                       dtype="float32", crs=gdf.crs, transform=transform,
                       nodata=np.nan) as dst:
        dst.write(grid, 1)
    burnt = int(np.count_nonzero(~np.isnan(grid)))
    facts: dict[str, Any] = {
        "ok": True, "output": _stamp(out, "rasterize", column or f"burn={burn}"),
        "features_in": len(gdf), "size": [width, height],
        "cells_burnt": burnt, "crs": str(gdf.crs) if gdf.crs else None,
    }
    if not burnt:
        facts["warning"] = (
            f"the raster is EMPTY: {len(gdf)} feature(s) went in and no cell was "
            f"burnt. A resolution of {resolution} is probably coarser than the "
            "features themselves.")
    return facts


def sample_raster(raster_path: str, points_path: str, output_path: str, *,
                  column: str = "value", workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Write the raster's value at each point into a new column.

    Points outside the raster, and points on a nodata cell, get ``null`` — never 0.
    The count of those is part of the answer, because "the raster does not reach
    there" and "the value is zero" are different facts.
    """
    import geopandas as gpd
    import numpy as np

    ws = workspace
    gdf = gpd.read_file(resolve_path(points_path, ws))
    with _open(raster_path, ws) as src:
        if gdf.crs is not None and src.crs is not None and gdf.crs != src.crs:
            gdf = gdf.to_crs(src.crs)
        coords = [(geom.x, geom.y) for geom in gdf.geometry.representative_point()]
        # `masked=True` ist der Unterschied zwischen „kein Wert" und „der Wert ist 0".
        # Ohne das liefert rasterio für einen Punkt **außerhalb** des Rasters den
        # Füllwert — bei einem Raster ohne gesetztes `nodata` also 0, und die Zahl
        # sieht aus wie eine Messung. Gemessen 2026-09-06 im eigenen Test.
        values: list[float | None] = []
        for v in src.sample(coords, masked=True):
            cell = v[0]
            val = float(cell)
            if np.ma.is_masked(cell) or np.isnan(val):
                values.append(None)
            else:
                values.append(val)
    out_gdf = gdf.copy()
    out_gdf[column] = values
    out = resolve_path(output_path, ws, write=True)
    out_gdf.to_file(out)
    missing = sum(1 for v in values if v is None)
    facts: dict[str, Any] = {
        "ok": True, "output": _stamp(out, "sample_raster", column),
        "features": len(out_gdf), "column": column, "without_value": missing,
    }
    if missing == len(values) and values:
        facts["warning"] = (
            "EVERY point came back without a value — the points and the raster do "
            "not overlap, or the whole area is nodata. Compare their CRS and bounds "
            "before using this column.")
    return facts


def zonal_stats(raster_path: str, zones_path: str, output_path: str, *,
                stat: str = "mean", column: str | None = None,
                workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Summarise the raster inside each zone polygon (mean/min/max/sum/count).

    Masks nodata out rather than averaging it in — a DEM's -9999 fill in a mean is
    the classic silently-wrong number. Each zone also reports ``coverage``, the share
    of its cells that carried data, so a zone the raster barely reaches is visible
    instead of merely quiet.
    """
    import geopandas as gpd
    import numpy as np
    from rasterio.mask import mask as rio_mask

    ws = workspace
    zones = gpd.read_file(resolve_path(zones_path, ws))
    field = column or f"{stat}_value"
    funcs: dict[str, Any] = {"mean": np.nanmean, "min": np.nanmin,
                             "max": np.nanmax, "sum": np.nansum}
    if stat not in funcs and stat != "count":
        return {"ok": False, "error": f"unknown stat {stat!r} — use "
                                      "mean, min, max, sum or count"}
    values: list[float | None] = []
    coverage: list[float | None] = []
    with _open(raster_path, ws) as src:
        if zones.crs is not None and src.crs is not None and zones.crs != src.crs:
            zones = zones.to_crs(src.crs)
        for geom in zones.geometry:
            try:
                clipped, _ = rio_mask(src, [geom], crop=True, filled=True,
                                      nodata=np.nan)
            except Exception:  # noqa: BLE001 - a zone outside the raster is a fact
                values.append(None)
                coverage.append(0.0)
                continue
            band = clipped[0].astype("float64")
            if src.nodata is not None:
                band = np.where(band == src.nodata, np.nan, band)
            valid = ~np.isnan(band)
            n_valid = int(valid.sum())
            coverage.append(round(n_valid / band.size, 4) if band.size else 0.0)
            if not n_valid:
                values.append(None)
            elif stat == "count":
                values.append(float(n_valid))
            else:
                values.append(float(funcs[stat](band[valid])))
    out_gdf = zones.copy()
    out_gdf[field] = values
    out_gdf["coverage"] = coverage
    out = resolve_path(output_path, ws, write=True)
    out_gdf.to_file(out)
    empty = sum(1 for v in values if v is None)
    facts: dict[str, Any] = {
        "ok": True, "output": _stamp(out, "zonal_stats", f"{stat}:{field}"),
        "zones": len(out_gdf), "column": field, "stat": stat,
        "zones_without_data": empty,
    }
    if empty == len(values) and values:
        facts["warning"] = (
            "NO zone got a value — the zones and the raster do not overlap, or the "
            "raster is entirely nodata. The column is null everywhere; do not report "
            "it as a result.")
    elif empty:
        facts["warning"] = (
            f"{empty} of {len(values)} zones got NO value (null, not 0) — the raster "
            "does not reach them. `coverage` says how much of each zone carried data.")
    return facts


def raster_calc(output_path: str, expression: str, *, workspace: str = DEFAULT_WORKSPACE,
                **rasters: str) -> dict:
    """Evaluate a numpy expression over aligned rasters, e.g. ``"(a - b) / (a + b)"``.

    Pass the bands as keyword arguments (``a="red.tif", b="nir.tif"``). All inputs
    must share a grid; a mismatch is refused rather than broadcast into nonsense.
    """
    import numpy as np
    import rasterio

    ws = workspace
    if not rasters:
        return {"ok": False, "error": "no rasters given — pass them as keywords, "
                                      'e.g. raster_calc(out, "a - b", a="x.tif", b="y.tif")'}
    scope: dict[str, Any] = {}
    profile: dict[str, Any] = {}
    shape: tuple[int, int] | None = None
    for name, path in rasters.items():
        with _open(path, ws) as src:
            band = src.read(1).astype("float64")
            if src.nodata is not None:
                band = np.where(band == src.nodata, np.nan, band)
            if shape is None:
                shape, profile = band.shape, dict(src.profile)
            elif band.shape != shape:
                return {"ok": False, "error": (
                    f"{name} is {band.shape} but the first raster is {shape} — the "
                    "grids differ. Align them (same extent and resolution) first; "
                    "numpy would otherwise broadcast into a wrong result.")}
            scope[name] = band
    try:
        grid = eval(expression, {"np": np, "__builtins__": {}}, scope)  # noqa: S307
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not evaluate {expression!r}: "
                                      f"{type(exc).__name__}: {exc}",
                "available": sorted(scope)}
    grid = np.asarray(grid, dtype="float32")
    out = resolve_path(output_path, ws, write=True)
    profile.update(dtype="float32", count=1, nodata=np.nan)
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(grid, 1)
    finite = grid[np.isfinite(grid)]
    if shape is None:  # nur erreichbar, wenn kein Raster gelesen werden konnte
        return {"ok": False, "error": "no raster could be read"}
    height, width = shape
    facts: dict[str, Any] = {
        "ok": True, "output": _stamp(out, "raster_calc", expression),
        "size": [int(width), int(height)],
        "range": [float(finite.min()), float(finite.max())] if finite.size else None,
    }
    if not finite.size:
        facts["warning"] = (
            "every cell is nodata or NaN — the expression produced nothing usable. "
            "A division by zero over the whole grid does this.")
    return facts


#: The four, by the name the snippet namespace uses — one place, like `geoops`.
OPERATIONS = {
    "rasterize": rasterize,
    "sample_raster": sample_raster,
    "zonal_stats": zonal_stats,
    "raster_calc": raster_calc,
}
