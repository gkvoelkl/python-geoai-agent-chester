"""One line of facts per produced layer, for an LLM judge that sees only the result.

`testprompt.layer_facts` shows the judge what a run produced, read from the files: CRS,
size, geometry type, extent. Since `CHESTER_JUDGE=result` (2026-10-24) that is all the
judge sees — and a criterion like "Pufferdistanz beträgt 500 m" was then unreadable: the
judge failed a correct run for "no distance information visible in the layers".

So polygon layers in a metric CRS also carry their **equivalent radius**, √(area/π): a
500 m buffer around a point has area π·500², radius 500. Reported as the most frequent
value in 10 m bins, with its count and the range — measured on the run that prompted this
(84 school buffers): median 530 m, because a buffer around a school *ground* comes out
larger and one clipped at the city edge smaller, but 25 of 84 sat at exactly 500 m (the
point schools). A fact, not a verdict: the judge reads it.
"""

from __future__ import annotations

import math
from pathlib import Path

from chester import geofacts

_POLYGONAL = {"Polygon", "MultiPolygon"}


def equivalent_radius(path: str) -> str | None:
    """√(area/π) of a polygon layer in a projected CRS, summarised; else ``None``."""
    from collections import Counter

    import geopandas as gpd

    gdf = gpd.read_file(path)
    if gdf.empty or gdf.crs is None or gdf.crs.is_geographic:
        return None
    polys = gdf[gdf.geometry.geom_type.isin(_POLYGONAL)]
    if polys.empty:
        return None
    radii = (polys.geometry.area / math.pi).pow(0.5)
    (mode, count), = Counter(int(round(r / 10)) * 10 for r in radii).most_common(1)
    return (f"equivalent radius √(area/π): most frequent ≈ {mode} m ({count} of {len(radii)}), "
            f"range {radii.min():.0f}–{radii.max():.0f} m")


def layer_line(path: str) -> str:
    """``name: CRS, N features, geometry, extent [...][, equivalent radius ...]``."""
    f = geofacts.vector_facts(path)
    types = f.get("geometry_types") or []
    parts = [f"{Path(path).name}: {f.get('crs') or 'no CRS'}", f"{f.get('feature_count')} features",
             "/".join(types) or "?", f"extent {f.get('bounds')}"]
    if set(types) & _POLYGONAL or "Unknown" in types:
        radius = equivalent_radius(path)
        if radius is not None:
            parts.append(radius)
    return ", ".join(parts)
