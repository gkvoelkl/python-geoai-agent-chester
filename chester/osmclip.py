"""Cut an OSM download down to the boundary it was asked for.

``osmnx.features_from_place`` returns every feature that *intersects* the named
place, with its geometry **uncut**: a forest that reaches into the city arrives
whole. Counting or measuring such a layer silently answers a different question
than the one asked — the Regensburg run of 2026-08-26 reported 35.06 km² of
green space for a city that holds 8.93 km², because one forest (Kreuther Forst,
25.84 km², of which 0.71 km² lie inside) came along in full.

The clip belongs here rather than in the capability so it can be tested without
a network call: pass a boundary geometry, get the trimmed frame plus a report of
what the trimming cost.
"""

from __future__ import annotations

from typing import Any

_M2_PER_KM2 = 1_000_000.0


def _polygon_area_m2(gdf: Any) -> float:
    """Total polygonal area in m², measured in a metric CRS.

    Geographic degrees are meaningless as an area, so project first; layers that
    carry no CRS are taken at face value (a caller that knows better clips
    anyway, and guessing a CRS here would be worse than reporting 0.0).
    """
    polys = gdf[gdf.geometry.geom_type.isin(("Polygon", "MultiPolygon"))]
    if polys.empty:
        return 0.0
    if polys.crs is None:
        return 0.0
    if polys.crs.is_geographic:
        polys = polys.to_crs(polys.estimate_utm_crs())
    return float(polys.geometry.area.sum())


def clip_to_boundary(gdf: Any, boundary: Any) -> tuple[Any, dict]:
    """Clip ``gdf`` to ``boundary``; return the trimmed frame and a report.

    ``boundary`` is a shapely geometry in the same CRS as ``gdf``. The report
    names what changed — how many features lost geometry, how many fell away
    entirely, and how much polygonal area lay outside — so the caller can say it
    out loud instead of quietly returning a different number.
    """
    import geopandas as gpd

    before_area = _polygon_area_m2(gdf)
    # keep_geom_type: a clipped polygon that touches the edge can come back as a
    # GeometryCollection; without this the layer changes type under the caller.
    clipped = gpd.clip(gdf, boundary, keep_geom_type=True)
    after_area = _polygon_area_m2(clipped)
    outside_m2 = max(0.0, before_area - after_area)

    # Row counts are not the measure here: clipping can *split* one feature into
    # several rows (a forest cut by a boundary that reaches around it), so a
    # plain len() difference reported -10 dropped features on Regensburg. Ask
    # the index instead — which inputs survived, and which produced more than one
    # piece.
    kept = gdf[gdf.index.isin(clipped.index)]
    # "Trimmed" is what the caller feels: a feature that is still there but
    # smaller. Comparing geometries object-wise is too slow on 25k features, so
    # compare against the boundary — a feature not fully covered by it was cut.
    trimmed = int((~kept.geometry.within(boundary)).sum()) if len(kept) else 0

    return clipped, {
        "clipped_to_place": True,
        "features_trimmed": trimmed,
        "features_dropped": len(gdf) - len(kept),
        "features_split": len(clipped) - len(set(clipped.index)),
        "area_outside_km2": round(outside_m2 / _M2_PER_KM2, 3),
    }


def clip_to_place(gdf: Any, place: str) -> tuple[Any, dict]:
    """Clip an OSM download to the admin polygon of ``place``.

    A failed boundary lookup must not lose the download: the features are kept
    uncut and the report says so, because a layer that is quietly *not* clipped
    while the docstring promises clipping is the defect this module exists to
    remove.
    """
    import osmnx as ox

    try:
        boundary = ox.geocode_to_gdf(place)
        return clip_to_boundary(gdf, boundary.geometry.iloc[0])
    except Exception as exc:  # noqa: BLE001 — any lookup failure degrades the same way
        return gdf, {
            "clipped_to_place": False,
            "clip_error": f"{type(exc).__name__}: {exc}",
        }


def clip_warning(report: dict, place: str) -> str | None:
    """The sentence that belongs in the tool's return value, or None.

    Silence when nothing was cut: a warning that fires on every call is one the
    reader learns to skip.
    """
    trimmed = report.get("features_trimmed", 0)
    dropped = report.get("features_dropped", 0)
    if not trimmed and not dropped:
        return None
    outside = report.get("area_outside_km2", 0.0)
    split = report.get("features_split", 0)
    parts = []
    if trimmed:
        parts.append(f"{trimmed} feature(s) crossed it and were cut")
    if dropped:
        parts.append(f"{dropped} feature(s) fell outside entirely")
    if split:
        parts.append(f"{split} cut(s) left a feature in more than one piece")
    if outside:
        parts.append(f"{outside} km2 of polygon area lay outside")
    return (
        f"clipped to the boundary of {place}: "
        + "; ".join(parts)
        + ". Areas and counts now refer to the named area itself. "
        "Pass clip=false to keep whole features (e.g. to map a forest that "
        "continues past the city limit)."
    )
