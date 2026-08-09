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
        left, bottom, right, top = tr.transform_bounds(*bounds)
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
        c for c in required
        if c not in fields
        or fields[c]["populated"] == 0
        or fields[c]["placeholder"] >= fields[c]["populated"]
    ]
    return {"row_count": n, "fields": fields, "missing_required": missing_required}


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
        sj = gpd.sjoin(valid[[valid.geometry.name]], valid[[valid.geometry.name]],
                       how="inner", predicate="overlaps")
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
_LENGTH_COLS = {"length", "length_m", "shape_leng", "shape_length", "st_length",
                "laenge", "länge", "len", "perimeter"}


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


def area_length_consistency(
    path: str, *, layer: str | None = None
) -> dict | None:
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
    both = pd.DataFrame(
        {"s": pd.to_numeric(gdf[col], errors="coerce"), "c": computed}
    ).dropna()
    both = both[both["c"] > 0]
    if both.empty:
        return None
    median_rel = float(((both["s"] - both["c"]).abs() / both["c"]).median())
    return {"kind": kind, "column": col, "median_rel_diff": median_rel,
            "n": int(both.shape[0])}


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
        key = formula.strip().lower()
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
            path, columns=[field], read_geometry=False,
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
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        .isoformat(timespec="seconds"),
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
