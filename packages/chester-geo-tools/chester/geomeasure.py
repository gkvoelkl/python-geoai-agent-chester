"""Measuring a layer and judging a raster's coverage — numbers that must not lie.

Split out of `geofacts.py` on 2026-10-05 (it had grown past 1000 lines). Two jobs,
both about whether a number can be trusted: `measure_layer`/`zone_summary` total a
column or geometry in one call, and the coverage readers say how much of a request or
a zone a raster actually covers (`raster_coverage`, `zone_coverage`) — a mean over 40 %
of a district is a number like any other unless someone says so — and whether a raster
holds anything at all (`raster_degenerate`). Pure readers; they raise on failure.
"""

from __future__ import annotations

from typing import Any


def _summary(values) -> dict:
    """sum/count/mean/min/max over a pandas numeric Series (NaN already dropped)."""
    n = int(values.shape[0])
    if n == 0:
        return {"sum": 0.0, "count": 0, "mean": None, "min": None, "max": None}
    return {
        "sum": float(values.sum()),
        "count": n,
        "mean": float(values.mean()),
        "min": float(values.min()),
        "max": float(values.max()),
    }


def measure_layer(
    path: str,
    *,
    field: str | None = None,
    formula: str | None = None,
    layer: str | None = None,
) -> dict | None:
    """In-process sum/count/mean/min/max — the fast path for ``vector_field_sum``.

    A total area/length or numeric-field sum is a one-line geopandas/pyogrio
    read here (seconds), versus two ``qgis_process`` passes over the whole layer
    plus an intermediate file (which times out at 100k+ features). Same
    in-process-facts principle as the rest of this module.

    Handles a numeric ``field`` total, or a geometry ``formula`` of ``$area`` /
    ``$length`` / ``$perimeter`` (measured in the layer's CRS units — the caller
    guards against a geographic CRS). Returns ``None`` when ``formula`` is any
    other QGIS expression this can't evaluate, so the caller falls back to the
    QGIS field calculator. Raises ``KeyError`` if a named ``field`` is absent.
    """
    import pandas as pd

    if formula:
        # Strip surrounding double quotes before matching: in QGIS syntax `"x"` is a
        # *field* reference, and a model that has read that rule writes `"$area"` —
        # which then falls through to the field calculator, finds no such column and
        # returns a silent 0 (observed 2026-08-22, benchmark run
        # supermarkets-within-10min-walk). No layer can hold a field named `$area`,
        # so reading the quoted form as the geometry variable loses nothing.
        key = formula.strip().strip('"').strip().lower()
        if key == "$area":
            measure = lambda geom: geom.area  # noqa: E731
        elif key in ("$length", "$perimeter"):
            measure = lambda geom: geom.length  # noqa: E731
        else:
            return None  # a real expression → let QGIS's field calculator do it
        import geopandas as gpd

        gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
        values = measure(gdf.geometry)
    else:
        # A numeric-field total: read just that column, skip geometry entirely.
        from pyogrio import read_dataframe

        df = read_dataframe(
            path,
            columns=[field],
            read_geometry=False,
            **({"layer": layer} if layer else {}),
        )
        if field not in df.columns:
            raise KeyError(field)
        values = pd.to_numeric(df[field], errors="coerce")

    return _summary(values.dropna())


def zone_summary(path: str, columns: list[str], *, name_hint: bool = True) -> dict | None:
    """Read a zonal-statistics result back and describe it in numbers.

    The tool that computes one value per zone used to return only the output
    path, so the answer that followed showed a map and talked about method —
    without a single figure. `mean-elevation-per-district` (2026-08-27) computed
    all eighteen district means correctly and then reported none of them.

    Returns per requested column: how many zones carry a value, min/max/mean, and
    the extremes *by name* where a name-like column exists — the shape an answer
    can be written from. ``None`` when the file cannot be read, so a summary that
    fails never costs the caller its result.
    """
    try:
        import geopandas as gpd

        gdf = gpd.read_file(path)
    except Exception:  # noqa: BLE001 — a read-back is a nicety, never the result
        return None

    # A name column makes "highest: Kager 396.6" possible instead of "max 396.6".
    name_col = None
    if name_hint:
        for cand in gdf.columns:
            if cand.lower() in ("name", "gen", "bezeichnung", "label", "title"):
                name_col = cand
                break

    out: dict = {}
    for col in columns:
        if col not in gdf.columns:
            continue
        vals = gdf[col].dropna()
        if vals.empty:
            out[col] = {"zones_with_value": 0}
            continue
        entry: dict[str, Any] = {
            "zones_with_value": int(vals.shape[0]),
            "min": round(float(vals.min()), 3),
            "max": round(float(vals.max()), 3),
            "mean": round(float(vals.mean()), 3),
        }
        if name_col:
            lo, hi = vals.idxmin(), vals.idxmax()
            entry["lowest"] = f"{gdf.loc[lo, name_col]}: {round(float(vals[lo]), 1)}"
            entry["highest"] = f"{gdf.loc[hi, name_col]}: {round(float(vals[hi]), 1)}"
        out[col] = entry
    return out or None


_COVERAGE_SAMPLE_PX = 512  # decimated read: a share needs no full-resolution scan


def raster_coverage(path: str, bbox: list[float] | None = None) -> dict | None:
    """Which share of the requested ``bbox`` this raster actually carries data for.

    Two ways to miss an area, and both look identical in a return value that only
    says ``ok: true``: the raster may not *reach* over the whole request (a state
    boundary cuts a DOP, a DEM tile is missing from the mosaic), and inside its
    extent it may be full of nodata. So the share is the product of both —
    ``extent_share`` × ``data_share`` over the requested window.

    Li, Ning et al. (2025) list "Does it adequately cover the study area?" among the
    uncertainties a data-aware GIS must resolve; a mean elevation over 60 % of a
    district is a plausible number with nothing to flag it.

    ``bbox`` is [west, south, east, north] in WGS84. Returns ``None`` when the file
    cannot be read — a missing share must never cost the caller its download.
    """
    try:
        import rasterio
        from rasterio.warp import transform_bounds
    except ImportError:
        return None
    try:
        with rasterio.open(path) as src:
            nodata = src.nodata
            left, bottom, right, top = src.bounds
            if bbox:
                # The request is WGS84; the raster may be metric. Compare in the
                # raster's own CRS so no reprojection of pixels is needed.
                want = transform_bounds("EPSG:4326", src.crs, *bbox, densify_pts=21)
            else:
                want = (left, bottom, right, top)
            wl, wb, wr, wt = want
            want_area = max(0.0, wr - wl) * max(0.0, wt - wb)
            inter = (
                max(0.0, min(wr, right) - max(wl, left))
                * max(0.0, min(wt, top) - max(wb, bottom))
            )
            extent_share = 1.0 if want_area <= 0 else min(1.0, inter / want_area)
            if inter <= 0:
                return {"covers_request": 0.0, "extent_share": 0.0, "data_share": 0.0}

            window = rasterio.windows.from_bounds(
                max(wl, left), max(wb, bottom), min(wr, right), min(wt, top),
                transform=src.transform,
            )
            h = max(1, min(_COVERAGE_SAMPLE_PX, int(window.height)))
            w = max(1, min(_COVERAGE_SAMPLE_PX, int(window.width)))
            band = src.read(1, window=window, out_shape=(h, w), boundless=False)
    except Exception:  # noqa: BLE001 — a coverage read is a nicety, never the result
        return None

    import numpy as np

    valid = np.isfinite(band)
    if nodata is not None:
        valid &= band != nodata
    data_share = float(valid.mean()) if valid.size else 0.0
    return {
        "covers_request": round(extent_share * data_share, 3),
        "extent_share": round(extent_share, 3),
        "data_share": round(data_share, 3),
    }


def coverage_warning(cov: dict | None, threshold: float = 0.98) -> str | None:
    """The sentence that belongs in a fetch tool's return value, or ``None``.

    Silent at full coverage: a warning on every call is one the reader skips.
    """
    if not cov or cov.get("covers_request", 1.0) >= threshold:
        return None
    share = cov["covers_request"]
    if cov.get("extent_share", 1.0) < threshold:
        why = f"the data reaches over only {cov['extent_share']:.0%} of it"
    else:
        why = f"{1 - cov.get('data_share', 1.0):.0%} of the covered part is nodata"
    return (
        f"this raster covers only {share:.0%} of the requested area — {why}. "
        "Any mean, sum or share computed over the missing part is silently based on "
        "what is there: check the extent, try another source or state, or say in the "
        "answer which part is not covered."
    )


def zone_coverage(
    zones_path: str, raster_path: str, count_column: str, *, threshold: float = 0.9
) -> dict | None:
    """Which zones the raster only partly filled — from the pixel count per zone.

    A zonal mean over 40 % of a district is a number like any other. The pixel
    count says what the mean does not: ``count`` against the pixels the zone's
    area *should* hold. Returns ``None`` when the two layers are in different CRS
    (the comparison would be meaningless) or the file cannot be read.
    """
    try:
        import geopandas as gpd
        import rasterio

        gdf = gpd.read_file(zones_path)
        with rasterio.open(raster_path) as src:
            if src.crs is None or gdf.crs is None or src.crs.to_epsg() != gdf.crs.to_epsg():
                return None
            pixel_area = abs(src.transform.a * src.transform.e)
    except Exception:  # noqa: BLE001 — a coverage check never costs the result
        return None
    if count_column not in gdf.columns or not pixel_area:
        return None

    name_col = next(
        (c for c in gdf.columns if c.lower() in ("name", "gen", "bezeichnung", "label")), None
    )
    partial: list[dict[str, Any]] = []
    for idx, row in gdf.iterrows():
        expected = float(row.geometry.area) / pixel_area if row.geometry is not None else 0.0
        if expected < 1:
            continue
        share = float(row[count_column] or 0) / expected
        if share < threshold:
            label = str(row[name_col]) if name_col else f"#{idx}"
            partial.append({"zone": label, "covered": round(min(1.0, share), 3)})
    partial.sort(key=lambda z: float(z["covered"]))
    return {
        "zones": int(len(gdf)),
        "partly_covered": partial[:10],
        "partly_covered_total": len(partial),
    }


def zone_coverage_warning(cov: dict | None) -> str | None:
    """The sentence for a partly covered set of zones, or ``None``."""
    if not cov or not cov.get("partly_covered_total"):
        return None
    worst = cov["partly_covered"][0]
    return (
        f"{cov['partly_covered_total']} of {cov['zones']} zones are only partly covered "
        f"by the raster (worst: {worst['zone']} at {worst['covered']:.0%}). Their values "
        "are computed from the covered part only — fetch the missing area or say which "
        "zones are affected."
    )


def raster_degenerate(path: str) -> str | None:
    """A raster of nothing but zeros or nothing but nodata is not a result.

    From operation, 2026-08-27: asked to mark four addresses on a map, a run produced a
    266 MB GeoTIFF **without a CRS** in which every pixel was 0, and a 355 MB mask, also
    0 throughout. The user saw a black area; every automatic check was satisfied. The
    finding needs no judgement — only a look at min and max.

    Returns the description of the defect, or ``None``. An unreadable raster also gives
    ``None``: the caller owns that (it reports "unreadable"), and a check must never
    break more than it finds.
    """
    try:
        import numpy as np
        import rasterio

        with rasterio.open(path) as src:
            h = max(1, min(_COVERAGE_SAMPLE_PX, src.height))
            w = max(1, min(_COVERAGE_SAMPLE_PX, src.width))
            band = src.read(1, out_shape=(h, w))
            nodata = src.nodata
    except Exception:  # noqa: BLE001 — the caller reports unreadable files
        return None

    valid = np.isfinite(band)
    if nodata is not None:
        valid &= band != nodata
    if not valid.any():
        return "every pixel is nodata/empty — the raster carries no data at all"
    lo, hi = float(band[valid].min()), float(band[valid].max())
    if lo != hi or lo != 0:
        # Deliberately narrow: reported are the **black area** (all 0) and the empty
        # raster (all nodata). Any *other* constant stays quiet — a SAVI flat at 2.4
        # over a small area is odd, not a defect, and a mandatory check must not force a
        # retry for it (caught by `test_gate_leaves_unbounded_indices_alone`).
        return None
    return (
        "every pixel is 0 — a black picture, not a finding; check the step that "
        "wrote it (a failed rasterisation, a wrong extent, a mask that matched nothing)"
    )
