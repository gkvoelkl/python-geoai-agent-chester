"""Quality facts about a produced layer: topology, dangles, and agreement with another.

Split out of `geofacts.py` on 2026-10-05 (it had grown past 1000 lines). Where
`geofacts` answers "what is in this file", this answers "is it sound": self-
intersections and overlaps (`topology_facts`), open line ends (`dangle_facts`), two
layers that should describe the same thing (`compare_layers`), and stored area/length
columns against the geometry (`area_length_consistency`). Pure readers like the rest —
they raise on failure, callers wrap them for the tool contract.
"""

from __future__ import annotations


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
