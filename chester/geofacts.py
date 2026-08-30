"""Shared geodata fact readers — one source of truth for "what is in this file".

Both the user-facing capabilities (``vector_info``, ``check_crs``,
``sanity_check_result``) and the GeoCache inventory (``geocache_sync``) need the
same facts about a dataset: CRS, bounds, feature count, geometry type, raster
size/bands. Extracting them here keeps the tool output and the inventory from
drifting.

Everything runs **in-process** over the installed geo stack
(geopandas / rasterio / pyogrio / pyproj) — never a ``qgis_process`` subprocess.
QGIS would be correct but far too slow: ``geocache_sync`` scans every file at
every gateway start, and a subprocess-per-file (Qt init + provider load) would
cost seconds each. The wheels here wrap the same GDAL/PROJ that QGIS uses.

Two depths of vector read:
- ``full=False`` (default for the inventory): metadata only via
  ``pyogrio.read_info`` — no geometries loaded, fast.
- ``full=True`` (for ``vector_info`` / ``sanity_check_result``): a real
  ``geopandas`` read, so populated columns and per-geometry validity can be
  reported.

The readers **raise** on failure (bad path, unreadable file); callers that need
the ``{"ok": False, "error": …}`` tool contract wrap them in try/except as they
already do.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RASTER_EXTS = {".tif", ".tiff", ".vrt", ".img", ".asc", ".jp2", ".dem"}
# Containers that may hold several layers behind one file path.
MULTILAYER_EXTS = {".gpkg", ".sqlite", ".db", ".gdb"}

# Sentinel/placeholder values that signal missing or failed attribute data (a
# failed join, a leaked raster nodata, an empty cell). Strings are compared
# stripped + lower-cased. The empty string is a placeholder here, but the
# validation gate passes a stricter set that excludes it — an OSM export has many
# legitimately-empty tag columns, and a mandatory retry on those would false-fire.
DEFAULT_PLACEHOLDER_STRINGS = {"", "null", "none", "nan", "n/a", "#n/a"}
DEFAULT_PLACEHOLDER_NUMBERS = {-9999.0, -99999.0}


def is_raster(path: str) -> bool:
    """True if ``path``'s extension is a known raster format."""
    return Path(path).suffix.lower() in RASTER_EXTS


def is_multilayer_container(path: str) -> bool:
    """True if ``path`` is a format that can carry multiple vector layers."""
    return Path(path).suffix.lower() in MULTILAYER_EXTS


def list_layers(path: str) -> list[str]:
    """Layer names inside a vector dataset (one entry for single-layer files).

    Uses ``pyogrio.list_layers`` — no geometries are read. Multi-layer
    containers (GeoPackage/SpatiaLite/FileGDB) expand to several names; a plain
    shapefile/GeoJSON returns a single name.
    """
    from pyogrio import list_layers as _list_layers

    info = _list_layers(path)  # ndarray of [name, geometry_type] rows
    return [str(row[0]) for row in info]


def _crs_string_and_geographic(crs) -> tuple[str | None, bool]:
    """Normalise any CRS input to ``(authority_string, is_geographic)``.

    Accepts a pyproj ``CRS``, a WKT/PROJ string, an EPSG code or ``None``.
    Prefers the compact ``AUTHORITY:CODE`` form (e.g. ``EPSG:25832``) and falls
    back to ``to_string()`` when no authority is known.
    """
    if not crs:
        return None, False
    from pyproj import CRS

    crs = CRS.from_user_input(crs)
    auth = crs.to_authority()
    text = f"{auth[0]}:{auth[1]}" if auth else crs.to_string()
    return text, bool(crs.is_geographic)


def _bounds_wgs84(bounds, crs) -> list[float] | None:
    """Reproject a native ``(minx, miny, maxx, maxy)`` box to EPSG:4326.

    Returns the box unchanged (rounded) when the source is already geographic,
    and ``None`` when there is no CRS or the transform fails — the inventory
    treats a missing WGS84 extent as "unknown", not an error.
    """
    if bounds is None or crs is None:
        return None
    from pyproj import CRS, Transformer

    src = CRS.from_user_input(crs)
    if src.is_geographic:
        return [round(float(b), 6) for b in bounds]
    try:
        tr = Transformer.from_crs(src, CRS.from_epsg(4326), always_xy=True)
        _w, _s, _e, _n = bounds
        left, bottom, right, top = tr.transform_bounds(_w, _s, _e, _n)
        out = [left, bottom, right, top]
        if any(v != v or v in (float("inf"), float("-inf")) for v in out):  # NaN/inf
            return None
        return [round(float(v), 6) for v in out]
    except Exception:  # noqa: BLE001 - WGS84 extent is best-effort
        return None


def populated_columns(gdf) -> list[str]:
    """Attribute columns of a GeoDataFrame that hold ≥1 non-null, non-empty value.

    OSM exports carry hundreds of mostly-empty tag columns; listing only the
    populated ones keeps schema reports useful.
    """
    geom = gdf.geometry.name
    cols = []
    for c in gdf.columns:
        if c == geom:
            continue
        s = gdf[c]
        if s.notna().any() and (s.astype("string").str.strip() != "").any():
            cols.append(c)
    return cols


def raster_facts(path: str) -> dict:
    """Metadata for a raster: size, bands, CRS, bounds (native + WGS84), nodata.

    ``rasterio.open`` reads only the header, so this is cheap even for large
    COGs.
    """
    import rasterio

    with rasterio.open(path) as ds:
        crs_text, is_geo = _crs_string_and_geographic(ds.crs)
        bounds = [ds.bounds.left, ds.bounds.bottom, ds.bounds.right, ds.bounds.top]
        return {
            "kind": "raster",
            "crs": crs_text,
            "is_geographic": is_geo,
            "width": ds.width,
            "height": ds.height,
            "bands": ds.count,
            "bounds": bounds,
            "bounds_wgs84": _bounds_wgs84(bounds, ds.crs),
            "nodata": ds.nodata,
            "resolution": [abs(ds.transform.a), abs(ds.transform.e)],
        }


def vector_facts(path: str, layer: str | None = None, *, full: bool = False) -> dict:
    """Metadata for a vector layer.

    ``full=False`` (default): a fast ``pyogrio.read_info`` read — CRS, feature
    count, geometry type and native bounds, no geometries loaded. Good for the
    inventory.

    ``full=True``: a real ``geopandas`` read so the result also carries
    ``columns`` (populated only) with dtypes, the distinct ``geometry_types``
    actually present, and null/empty/invalid geometry counts. Good for
    ``vector_info`` / ``sanity_check_result``.
    """
    if not full:
        from pyogrio import read_info

        info = read_info(path, layer=layer) if layer else read_info(path)
        crs_text, is_geo = _crs_string_and_geographic(info.get("crs"))
        tb = info.get("total_bounds")
        bounds = [float(b) for b in tb] if tb is not None else None
        geom_type = info.get("geometry_type")
        return {
            "kind": "vector",
            "crs": crs_text,
            "is_geographic": is_geo,
            "feature_count": int(info.get("features", 0)),
            "geometry_types": [geom_type] if geom_type else [],
            "bounds": [round(b, 6) for b in bounds] if bounds else None,
            "bounds_wgs84": _bounds_wgs84(bounds, info.get("crs")),
            "layer": layer or info.get("layer_name"),
        }

    import geopandas as gpd

    gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    crs_text, is_geo = _crs_string_and_geographic(gdf.crs)
    geom = gdf.geometry
    geom_types = sorted({g.geom_type for g in geom if g is not None})
    populated = populated_columns(gdf)
    attr_cols = [c for c in gdf.columns if c != gdf.geometry.name]
    native_bounds = [float(b) for b in gdf.total_bounds.tolist()]
    return {
        "kind": "vector",
        "crs": crs_text,
        "is_geographic": is_geo,
        "feature_count": len(gdf),
        "geometry_types": geom_types,
        "bounds": [round(b, 3) for b in native_bounds],
        "bounds_wgs84": _bounds_wgs84(native_bounds, gdf.crs),
        "columns": {c: str(gdf[c].dtype) for c in populated},
        "columns_total": len(attr_cols),
        "columns_empty": len(attr_cols) - len(populated),
        "geom_null": int(geom.isna().sum()),
        "geom_empty": int(sum(1 for g in geom if g is not None and g.is_empty)),
        "geom_invalid": int(sum(1 for g in geom if g is not None and not g.is_valid)),
    }


def _count_placeholders(series, placeholder_strings, placeholder_numbers) -> int:
    """How many non-null values in ``series`` are a placeholder/sentinel."""
    import pandas as pd

    nonnull = series.dropna()
    if nonnull.empty:
        return 0
    as_str = nonnull.astype("string").str.strip().str.lower()
    hits = as_str.isin(placeholder_strings)
    if placeholder_numbers:
        num = pd.to_numeric(nonnull, errors="coerce")
        hits = hits | num.isin(list(placeholder_numbers))
    return int(hits.sum())


def attribute_facts(
    path: str,
    *,
    layer: str | None = None,
    required=(),
    ranges: dict | None = None,
    placeholder_strings=None,
    placeholder_numbers=None,
) -> dict:
    """Per-field completeness facts for a vector layer's attributes (V1).

    Reads the attribute table only (``read_geometry=False`` — no geometries, so
    it's cheap) and reports, for each field: null count, placeholder/sentinel
    count, out-of-range count (against ``ranges={field: (min, max)}``), how many
    values are populated, and ``all_placeholder`` (every populated value is a
    sentinel — the strong "failed join / leaked nodata" signal). ``missing_required``
    lists fields from ``required`` that are absent or effectively empty.

    Placeholder sets default to the module constants; callers (the gate) may pass a
    stricter set. A pure reader like the rest of this module — callers wrap it for
    the ``{"ok": False, ...}`` tool contract.
    """
    import pandas as pd
    from pyogrio import read_dataframe

    ps = DEFAULT_PLACEHOLDER_STRINGS if placeholder_strings is None else set(placeholder_strings)
    pn = DEFAULT_PLACEHOLDER_NUMBERS if placeholder_numbers is None else set(placeholder_numbers)
    ranges = ranges or {}

    df = read_dataframe(path, read_geometry=False, **({"layer": layer} if layer else {}))
    n = len(df)
    fields: dict[str, dict] = {}
    for col in df.columns:
        s = df[col]
        null = int(s.isna().sum())
        placeholder = _count_placeholders(s, ps, pn)
        populated = n - null
        out_of_range = 0
        if col in ranges:
            lo, hi = ranges[col]
            num = pd.to_numeric(s, errors="coerce")
            out_of_range = int(((num < lo) | (num > hi)).sum())
        fields[col] = {
            "null": null,
            "placeholder": placeholder,
            "populated": populated,
            "out_of_range": out_of_range,
            "all_placeholder": populated > 0 and placeholder >= populated,
        }
    missing_required = [
        c
        for c in required
        if c not in fields
        or fields[c]["populated"] == 0
        or fields[c]["placeholder"] >= fields[c]["populated"]
    ]
    return {"row_count": n, "fields": fields, "missing_required": missing_required}


def column_values(path: str, column: str, *, layer: str | None = None, limit: int = 50) -> dict:
    """The distinct values of one attribute column, in the layer's own order.

    Answers "which of these features is the one I mean?" — the question that
    otherwise becomes a PyQGIS loop over ``getFeatures()``. Reads that column
    without geometries (``read_geometry=False``), so asking it of a 40k-feature OSM
    layer is cheap.

    Returns ``{"column", "distinct", "values", "truncated"}``, or ``{"column",
    "error", "available_columns"}`` when the column is absent — a missing column is
    a question to answer, not an exception to raise.
    """
    from pyogrio import read_dataframe

    kwargs = {"layer": layer} if layer else {}
    try:
        df = read_dataframe(path, read_geometry=False, columns=[column], **kwargs)
    except Exception:  # noqa: BLE001 - older/other drivers ignore `columns`
        df = read_dataframe(path, read_geometry=False, **kwargs)
    if column not in df.columns:
        available = read_dataframe(path, read_geometry=False, max_features=1, **kwargs)
        return {
            "column": column,
            "error": f"no column '{column}' in this layer",
            "available_columns": list(available.columns)[:40],
        }
    # dict.fromkeys keeps first-seen order: the layer's own order beats an
    # alphabetical one when looking for a particular feature.
    values = list(dict.fromkeys(df[column].dropna().astype(str)))
    return {
        "column": column,
        "distinct": len(values),
        "values": values[:limit],
        "truncated": len(values) > limit,
    }


def _count_holes(geom) -> int:
    """Interior rings across a (Multi)Polygon — holes in a merged coverage."""
    from shapely.geometry import MultiPolygon, Polygon

    if isinstance(geom, Polygon):
        return len(geom.interiors)
    if isinstance(geom, MultiPolygon):
        return sum(len(p.interiors) for p in geom.geoms)
    return 0


def _pairwise_topology(gdf) -> dict:
    """The heavier self-join part of ``topology_facts`` (overlaps + coverage holes).

    Split out so ``topology_facts`` can skip it above a feature cap. Uses a spatial
    self-join (STRtree-backed) rather than an O(n²) scan; ``predicate="overlaps"``
    is genuine partial overlap (shared *edges* are ``touches``, containment is
    ``contains``/``within``, exact duplicates are ``equals`` — none count here).
    """
    import geopandas as gpd

    facts: dict = {"overlap_checked": True, "self_overlaps": None, "union_holes": None}
    valid = gdf[gdf.geometry.notna() & gdf.geometry.is_valid]
    valid = valid.reset_index(drop=True)
    try:
        sj = gpd.sjoin(
            valid[[valid.geometry.name]],
            valid[[valid.geometry.name]],
            how="inner",
            predicate="overlaps",
        )
        left = sj.index.to_numpy()
        right = sj["index_right"].to_numpy()
        facts["self_overlaps"] = int((left < right).sum())  # unordered distinct pairs
    except Exception:  # noqa: BLE001 - the overlap scan is best-effort
        pass
    try:
        if set(valid.geometry.geom_type) & {"Polygon", "MultiPolygon"}:
            merged = valid.geometry.union_all()
            facts["union_holes"] = _count_holes(merged)
    except Exception:  # noqa: BLE001 - the gap scan is best-effort
        pass
    return facts


def topology_facts(
    path: str,
    *,
    layer: str | None = None,
    check_overlaps: bool = True,
    max_overlap_features: int = 20_000,
) -> dict:
    """Topological facts for a vector layer (V2, in-process — never ``qgis_process``).

    Always-cheap per-geometry facts: ``invalid`` (self-intersections / ring errors,
    shapely ``is_valid``), ``not_simple`` (self-crossing lines, ``is_simple``) and
    ``duplicate_geometries`` (exact repeats). The heavier **pairwise** part —
    ``self_overlaps`` (overlapping feature pairs) and ``union_holes`` (holes in the
    merged coverage, i.e. potential gaps) — runs only when ``check_overlaps`` and the
    layer is ≤ ``max_overlap_features`` (``overlap_checked`` says whether it ran);
    both are ``None`` when skipped. A pure reader like the rest of this module.
    """
    import geopandas as gpd

    gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    geom = gdf.geometry
    nonnull = geom[~geom.isna()]
    facts = {
        "feature_count": len(gdf),
        "invalid": int(sum(1 for g in nonnull if not g.is_valid)),
        "not_simple": int(sum(1 for g in nonnull if not g.is_simple)),
        "duplicate_geometries": int(nonnull.duplicated().sum()),
        "self_overlaps": None,
        "union_holes": None,
        "overlap_checked": False,
    }
    if check_overlaps and len(gdf) <= max_overlap_features:
        facts.update(_pairwise_topology(gdf))
    return facts


# Stored area/length columns Chester's connectors / QGIS / other GIS tools emit,
# recognised for the geometry-vs-attribute cross-check (case-insensitive).
_AREA_COLS = {"area", "area_m2", "area_sqm", "shape_area", "st_area", "flaeche", "fläche"}
_LENGTH_COLS = {
    "length",
    "length_m",
    "shape_leng",
    "shape_length",
    "st_length",
    "laenge",
    "länge",
    "len",
    "perimeter",
}


def compare_layers(
    path_a: str,
    field_a: str,
    path_b: str,
    field_b: str,
    key: str,
    *,
    layer_a: str | None = None,
    layer_b: str | None = None,
) -> dict:
    """Join two layers on ``key`` and compare two numeric columns (V5 two-method).

    The redundancy building block: a *second method* for the same quantity should
    roughly agree (LoD2 ``measured_height`` vs DSM−DTM, a stats table vs a second
    source). Returns match count + absolute/relative difference distribution. Raises
    ``KeyError`` if the key or a value column is missing.
    """
    import pandas as pd
    from pyogrio import read_dataframe

    da = read_dataframe(path_a, read_geometry=False, **({"layer": layer_a} if layer_a else {}))
    db = read_dataframe(path_b, read_geometry=False, **({"layer": layer_b} if layer_b else {}))
    for df, name, col in ((da, "a", field_a), (db, "b", field_b)):
        if key not in df.columns:
            raise KeyError(f"key '{key}' not in layer {name}")
        if col not in df.columns:
            raise KeyError(f"field '{col}' not in layer {name}")

    left = da[[key, field_a]].rename(columns={field_a: "_a"})
    right = db[[key, field_b]].rename(columns={field_b: "_b"})
    m = left.merge(right, on=key, how="inner")
    a = pd.to_numeric(m["_a"], errors="coerce")
    b = pd.to_numeric(m["_b"], errors="coerce")
    diff = (a - b).abs()
    rel = diff / b.abs().replace(0, pd.NA)
    valid = diff.dropna()
    rel_valid = rel.dropna()
    return {
        "matched": int(len(m)),
        "compared": int(valid.shape[0]),
        "mean_abs_diff": float(valid.mean()) if len(valid) else None,
        "median_abs_diff": float(valid.median()) if len(valid) else None,
        "max_abs_diff": float(valid.max()) if len(valid) else None,
        "mean_rel_diff": float(rel_valid.mean()) if len(rel_valid) else None,
        "max_rel_diff": float(rel_valid.max()) if len(rel_valid) else None,
    }


def area_length_consistency(path: str, *, layer: str | None = None) -> dict | None:
    """Compare a stored area/length column to the geometry (V5, gate auto-check).

    A two-method agreement that needs no external source: a stored ``area``/``length``
    attribute should match the recomputed geometric measure; a large gap means a
    stale attribute (edited/reprojected since) or wrong units. Returns ``None`` when
    there is no recognisable area/length column or no metric CRS (can't recompute),
    else the median relative difference between stored and geometric values.
    """
    import geopandas as gpd
    import pandas as pd

    gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    if gdf.crs is None or gdf.crs.is_geographic:
        return None
    cols = {c.lower(): c for c in gdf.columns}
    area_col = next((cols[c] for c in _AREA_COLS if c in cols), None)
    length_col = next((cols[c] for c in _LENGTH_COLS if c in cols), None)
    if area_col:
        kind, col, computed = "area", area_col, gdf.geometry.area
    elif length_col:
        kind, col, computed = "length", length_col, gdf.geometry.length
    else:
        return None
    both = pd.DataFrame({"s": pd.to_numeric(gdf[col], errors="coerce"), "c": computed}).dropna()
    both = both[both["c"] > 0]
    if both.empty:
        return None
    median_rel = float(((both["s"] - both["c"]).abs() / both["c"]).median())
    return {"kind": kind, "column": col, "median_rel_diff": median_rel, "n": int(both.shape[0])}


def dangle_facts(
    path: str,
    *,
    layer: str | None = None,
    tolerance: float = 0.0,
    max_dangle_length: float | None = None,
) -> dict | None:
    """Free line ends (dangles) in a line network — in-process (no GRASS needed).

    Network topology the pairwise checks in ``topology_facts`` don't cover: a
    **dangle** is a line end that connects to nothing. Endpoints are snapped (to
    ``tolerance``, else rounded to ~µm) and node **degree** is counted; a node touched
    by exactly one line end is a *free end*. Every free end is reported, and — because
    a road network has many legitimate dead-ends — ``max_dangle_length`` optionally
    counts only *short* free-ended lines (likely digitising overshoots/undershoots),
    the same idea as GRASS ``v.clean tool=rmdangle threshold=…``. Returns ``None`` if
    the layer carries no line geometry. A pure reader like the rest of this module.

    Note the QGIS build here lists the GRASS provider but has no runnable GRASS
    backend, so ``grass:v.clean`` can't execute — this is the in-process equivalent.
    """
    from collections import Counter

    import geopandas as gpd

    gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)

    def _snap(pt):
        if tolerance > 0:
            return (round(pt[0] / tolerance) * tolerance, round(pt[1] / tolerance) * tolerance)
        return (round(pt[0], 6), round(pt[1], 6))

    degree: Counter = Counter()
    lines: list[tuple] = []  # (start_node, end_node, length)
    for geom in gdf.geometry:
        if geom is None or geom.geom_type not in ("LineString", "MultiLineString"):
            continue
        parts = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
        for part in parts:
            coords = list(part.coords)
            if len(coords) < 2:
                continue
            a, b = _snap(coords[0]), _snap(coords[-1])
            degree[a] += 1
            degree[b] += 1
            lines.append((a, b, part.length))

    if not lines:
        return None

    free_ends = sum(1 for node, d in degree.items() if d == 1)
    free_lines = [(a, b, length) for a, b, length in lines if degree[a] == 1 or degree[b] == 1]
    short = None
    if max_dangle_length is not None:
        short = sum(1 for _a, _b, length in free_lines if length <= max_dangle_length)
    return {
        "line_count": len(lines),
        "nodes": len(degree),
        "free_ends": free_ends,
        "free_end_lines": len(free_lines),
        "short_dangles": short,
        "max_dangle_length": max_dangle_length,
    }


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
    """In-process sum/count/mean/min/max — the fast path for ``qgis_field_sum``.

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


def file_stat(path: str) -> dict:
    """Filesystem facts the inventory tracks for change detection: size + mtime."""
    st = os.stat(path)
    return {
        "size_bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
    }


def dataset_facts(path: str, layer: str | None = None) -> dict:
    """One inventory entry's worth of facts: kind-dispatched metadata + file stat.

    Vector layers use the fast (metadata-only) ``vector_facts`` path. The caller
    is responsible for enumerating layers of a multi-layer container (see
    ``list_layers``) and calling this once per layer.
    """
    facts = raster_facts(path) if is_raster(path) else vector_facts(path, layer)
    facts.update(file_stat(path))
    return facts


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
