"""Helfer, die **mehr als eine** Werkzeuggruppe der Datenbeschaffung braucht.

Phase KM, Schritt 1: Beim Schneiden von `capabilities/discovery.py` nach Themen zeigte
sich, dass ein paar Helfer quer liegen — `_wfs_base_and_typename` etwa braucht sowohl
die Katalogsuche (um einen Treffer als benutzbaren WFS zu erkennen) als auch die
OGC-Werkzeuge selbst. Sie hier abzulegen ist die Alternative dazu, dass ein reines
Hüllenmodul aus der Capability-Schicht importiert — was die Richtung capabilities → core
umdrehen würde und `tests/test_structure.py` zu Recht verbietet.
"""

from __future__ import annotations

from urllib.parse import urlencode

_WFS_REQ_PARAMS = {
    "service",
    "version",
    "request",
    "typename",
    "typenames",
    "outputformat",
    "srsname",
    "bbox",
    "maxfeatures",
    "count",
    "resulttype",
    "propertyname",
}

def _wfs_base_and_typename(url: str) -> tuple[str, str | None]:
    """Split a WFS GetFeature URL into its base service URL and typename.

    Catalogs often list a ready-made GetFeature request; wfs_features /
    wfs_capabilities want the plain service endpoint (keeping e.g. MapServer's
    ``map=`` param) plus the typename separately.
    """
    from urllib.parse import parse_qsl, urlparse, urlunparse

    parsed = urlparse(url)
    typename, keep = None, []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k.lower() in ("typename", "typenames"):
            typename = v
        if k.lower() in _WFS_REQ_PARAMS:
            continue
        keep.append((k, v))
    return urlunparse(parsed._replace(query=urlencode(keep))), typename

_OSM_LICENCE = "© OpenStreetMap contributors (ODbL)"

def _saveable(gdf):
    """Prepare an OSM GeoDataFrame so any file driver accepts it.

    Two OSM quirks break naive writes:

    * Case-colliding columns — mappers spell the same tag both ways
      (``fixme``/``FIXME``), so geopandas holds two columns. GeoPackage (SQLite)
      treats identifiers case-insensitively and refuses the duplicate. Each
      case-insensitive group is merged into one column (first-seen name wins;
      later columns fill only where the winner is null), so GPKG/Shapefile/…
      accept it — GeoJSON tolerated the pair, other drivers don't.
    * List/dict-valued cells — some tags parse to lists; those are joined to
      strings the driver can store.
    """
    out = gdf.copy()
    geom = out.geometry.name

    groups: dict[str, list[str]] = {}
    for col in out.columns:
        if col == geom:
            continue
        groups.setdefault(col.casefold(), []).append(col)
    for cols in groups.values():
        if len(cols) == 1:
            continue
        keep, *rest = cols
        for other in rest:
            missing = out[keep].isna()
            out.loc[missing, keep] = out.loc[missing, other]
        out = out.drop(columns=rest)

    for col in out.columns:
        if col == geom:
            continue
        if out[col].map(lambda v: isinstance(v, (list, set, dict))).any():
            out[col] = out[col].map(
                lambda v: ";".join(map(str, v)) if isinstance(v, (list, set)) else str(v)
            )
    return out

def _apply_where(gdf, where: dict):
    """Keep rows matching every column→value pair in ``where`` (case-insensitive,
    exact match on the stringified value).

    Returns ``(filtered_gdf, missing_columns)``; a missing column is reported
    rather than silently matching nothing.
    """
    missing = [c for c in where if c not in gdf.columns]
    if missing:
        return gdf, missing
    mask = None
    for col, value in where.items():
        col_norm = gdf[col].astype("string").str.strip().str.casefold()
        # fillna(False): null cells must not poison the boolean mask with <NA>.
        m = (col_norm == str(value).strip().casefold()).fillna(False)
        mask = m if mask is None else (mask & m)
    return gdf[mask], []
