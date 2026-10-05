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


# The three families a geometry can belong to. Multi- and single-part are the same
# family: whether QGIS hands back Polygon or MultiPolygon depends on the algorithm
# and the writer, and a check that turns on that coin flip is worse than none
# (measured 2026-08-23, `buffer-schools-500m`).
GEOMETRY_FAMILY = {"Point": "point", "MultiPoint": "point",
                   "LineString": "line", "MultiLineString": "line",
                   "LinearRing": "line",
                   "Polygon": "polygon", "MultiPolygon": "polygon"}


def is_raster(path: str) -> bool:
    """True if ``path``'s extension is a known raster format."""
    return Path(path).suffix.lower() in RASTER_EXTS


def geometry_families(path: str) -> set[str]:
    """Which of point/line/polygon really sit in a vector file — empty if unreadable.

    Read from the geometries, never from the header. A GeoPackage records **one**
    declared type and a mixed layer therefore announces whatever was written first:
    `supermarkets_25832.gpkg` says `Point` while holding 109 points and 138 polygons
    (measured 2026-08-19).
    """
    if not isinstance(path, str) or not os.path.isfile(path) or is_raster(path):
        return set()
    try:
        from pyogrio import read_dataframe

        frame = read_dataframe(path, columns=[], read_geometry=True)
        types = getattr(frame, "geom_type", None)
        if types is None:  # a table without geometry (CSV/XLSX)
            return set()
        return {GEOMETRY_FAMILY[t] for t in set(types.dropna()) if t in GEOMETRY_FAMILY}
    except Exception:  # noqa: BLE001 - a fact reader used by checks must not throw
        return set()


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

    Works on a plain table too (a CSV read through geopandas comes back as a
    DataFrame): there is simply no geometry column to skip.
    """
    try:
        geom = gdf.geometry.name
    except AttributeError:
        geom = None
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


def _table_facts(df) -> dict:
    """The same keys as a vector layer, for a table without geometry.

    Same shape so callers need not tell them apart: `crs`, `bounds` and
    `geometry_types` are empty, `kind` says why. The dtype per column is the point — an
    AGS as ``int64`` beside an AGS as ``object`` is half the diagnosis of a failed join.
    """
    populated = populated_columns(df)
    return {
        "kind": "table",
        "crs": None,
        "is_geographic": False,
        "feature_count": len(df),
        "geometry_types": [],
        "bounds": None,
        "bounds_wgs84": None,
        "columns": {c: str(df[c].dtype) for c in populated},
        "columns_total": len(df.columns),
        "columns_empty": len(df.columns) - len(populated),
        "geom_null": 0,
        "geom_empty": 0,
        "geom_invalid": 0,
        "note": "Table without geometry — no CRS, no extent. The column dtypes "
                "are what decides a join.",
    }


def mixed_geometry_note(geometry_types) -> str | None:
    """What to say when a layer holds several geometry families — or ``None``.

    One place for the text, because several tools need it: `vector_info` when looking
    and the download tools (`osm_features` & co.) when producing. Measured 2026-09-05
    (`supermarket-accessibility-choropleth`): the agent called `vector_info` **not once**
    in that flow — the note there never reached it. It learned it from `geometry_types`
    in the `osm_features` return, i.e. where the layer is born. That is where the
    consequence belongs too.
    """
    families = {GEOMETRY_FAMILY[t] for t in (geometry_types or []) if t in GEOMETRY_FAMILY}
    if len(families) < 2:
        return None
    return (
        f"MIXED GEOMETRY: this one layer holds {len(families)} geometry families "
        f"({', '.join(sorted(families))}). OSM maps the same thing as a point or "
        "as an area depending on size — larger shops are buildings, smaller ones "
        "points. A QGIS algorithm writes ONE type and drops the rest without a "
        "word, and every step that passes this layer through leaves a file whose "
        "header names only one type. Separate the types BEFORE counting or "
        "overlaying. To COUNT everything, native:centroids turns the areas into "
        "points in one call. When the families must be treated DIFFERENTLY (areas "
        "measured from the polygons, points only counted), "
        "`vector_split_by_geometry` writes one file per geometry type and changes "
        "nothing else — a centroid has no area, so anything MEASURED from it "
        "(area, distance, a buffer's reach) comes out too small."
    )


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
    try:
        crs_text, is_geo = _crs_string_and_geographic(gdf.crs)
        geom = gdf.geometry
    except AttributeError:
        # A table without geometry — a CSV, say. `gpd.read_file` returns a plain
        # DataFrame for it, which has neither `.crs` nor `.geometry`. Until 2026-09-05
        # that surfaced as `AttributeError: 'DataFrame' object has no attribute 'crs'`
        # in the tool answer: before a join the agent only wanted to know which
        # columns the CSV has — the most obvious question there is — got a Python
        # error and fell back on hand-written pandas (`join-leading-zero-ags`). The
        # column names and their types are exactly what a join needs; returning them
        # is not a workaround but the answer.
        return _table_facts(gdf)

    geom_types = sorted({g.geom_type for g in geom if g is not None})
    populated = populated_columns(gdf)
    attr_cols = [c for c in gdf.columns if c != gdf.geometry.name]
    native_bounds = [float(b) for b in gdf.total_bounds.tolist()]
    facts = {
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
    note = mixed_geometry_note(geom_types)
    if note:
        facts["mixed_geometry"] = True
        facts["note"] = note
    return facts


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
    stricter set. A pure reader — callers wrap it for the ``{"ok": False}`` contract.
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
            "numeric": bool(pd.api.types.is_numeric_dtype(s)),
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


