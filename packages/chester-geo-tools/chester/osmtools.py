"""OSM retrieval as a **framework-neutral** wrapper.

Phase KM, step 1, the last cut from `capabilities/discovery.py`. The tool with the most
traps of the whole file: `osm_features` clips a named place to the official boundary
(instead of taking the bbox), warns on OR-combined tags, treats `"yes"` and `True` alike
and reports mixed geometries. That behaviour is why the rules need not travel as prose
(`internal/chester-mcp.md` §4b) — it lives in the tool.
"""

from __future__ import annotations

from collections.abc import Callable

from chester import provenance
from chester.discoveryshared import (
    _OSM_LICENCE,
    _apply_where,
    _saveable,
    instructions,  # noqa: F401  # the group shares one text
)
from chester.geofacts import mixed_geometry_note
from chester.osmclip import clip_to_place, clip_warning
from chester.workspace import resolve_path


def _stringify_tag_value(value):
    """Coerce an OSM tag value into what osmnx accepts (bool / str / list of str).

    The model naturally writes numeric OSM values as numbers — ``admin_level: 8``,
    ``layer: -1`` — but osmnx rejects anything that isn't a bool, str, or list of
    str. Turn ints/floats into their string form (``8`` → ``"8"``; a whole float
    ``8.0`` → ``"8"``) so a common, correct query isn't a type error.
    """
    if isinstance(value, bool):  # bool is an int subclass — keep it as a bool
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, list):
        return [_stringify_tag_value(v) for v in value]
    return value

def _stringify_tags(tags: dict) -> dict:
    """Apply :func:`_stringify_tag_value` to every tag value."""
    return {k: _stringify_tag_value(v) for k, v in tags.items()}

def _or_tags_warning(gdf, tags: dict) -> str:
    """Warn when a multi-key tag query returned rows matching only *some* keys.

    osmnx unions multiple tag keys. Measured on Regensburg: ``boundary=administrative``
    41 features, ``admin_level=8`` 21, both **42** — the union; the intersection is 20.
    In `voronoi-catchment` (2026-08-26) the agent followed Chester's own docstring and
    clipped a city analysis against the Landkreis, the Bezirk and nine neighbours.

    Reported rather than silently intersected: a union is sometimes what the caller
    wants (all shops *or* all cafés), and rewriting a query behind the caller's back
    is worse than an honest note. ``where`` does the intersection when it is wanted.
    """
    if len(tags) < 2 or gdf is None or len(gdf) == 0:
        return ""
    partial = 0
    for _, row in gdf.iterrows():
        for key, value in tags.items():
            present = key in gdf.columns and row.get(key) is not None and str(
                row.get(key)) != "nan"
            if value is True:
                if not present:
                    partial += 1
                    break
            elif not present or str(row.get(key)).strip() != str(value).strip():
                partial += 1
                break
    if not partial:
        return ""
    keys = ", ".join(repr(k) for k in tags)
    first = next(iter(tags))
    rest = {k: v for k, v in tags.items() if k != first}
    return (
        f" — NOTE: {partial} of {len(gdf)} features do NOT match all of {keys}. "
        f"Multiple tag keys are combined with OR, not AND, so this layer is a union. "
        f"For 'all of them' pass one tag and filter the rest: "
        f"tags={{{first!r}: {tags[first]!r}}}, where={rest!r}."
    )

def _quoted_boolean_hint(tags: dict) -> str:
    """Name tag values written as the *string* "true"/"false" instead of the bool.

    `{"highway": "true"}` asks OSM for ways literally tagged ``highway=true``, of
    which there are none — the intent was `{"highway": true}`, "any highway". The
    two differ by two quotation marks and produce identical-looking calls.

    Not coerced, only reported: ``"true"`` is a legal tag value, and a connector that
    rewrites a query stops being trustworthy. In `walk-isochrone-hauptbahnhof`
    (2026-08-25) the empty answer made the agent rebuild the network from `footway` +
    `path` alone — 635 of 1844 km, every residential street missing.
    """
    quoted = [k for k, v in tags.items() if isinstance(v, str) and v.lower() in ("true", "false")]
    if not quoted:
        return ""
    shown = ", ".join(f'"{k}": "{tags[k]}"' for k in quoted)
    fixed = ", ".join(f'"{k}": {str(tags[k]).lower()}' for k in quoted)
    return (
        f" — NOTE: {shown} passes the *string*, which matches only ways literally "
        f"tagged that way. For \"any value of this key\" pass the boolean: {fixed}."
    )



def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """The 1 tools of this group, bound to ``workspace``."""
    ws = workspace

    def osm_features(
        tags: dict,
        output_path: str,
        place: str | None = None,
        bbox: list[float] | None = None,
        where: dict | None = None,
        max_features: int | None = None,
        clip: bool = True,
    ) -> dict:
        """Download OSM features matching ``tags`` as a GeoJSON layer (WGS84).

        Provide either ``place`` (e.g. "Bonn, Germany" — prefer it for named
        areas) or ``bbox`` as
        [west, south, east, north]. tags examples: {"building": true} for
        buildings, {"highway": true} for roads, {"natural": "water"} for water.

        **Several tag keys are OR, never AND** — `{"boundary": "administrative",
        "admin_level": 8}` returns everything administrative *plus* everything at
        level 8, i.e. the Landkreis and the neighbouring Gemeinden along with the
        city. For administrative boundaries pass one tag and intersect with
        ``where``: ``tags={"boundary": "administrative"},
        where={"admin_level": "8"}``. In Germany admin_level 6 = Landkreis/Kreis,
        7 = Verwaltungsgemeinschaft, 8 = Gemeinde/Stadt. Numeric tag values (an
        int like ``admin_level: 8``) are accepted and coerced to strings. For a
        single city boundary, ``geocode(query, output_path=…)`` is simpler still.

        **A feature is an OSM element, not a real-world object.** One bridge is
        often several ways (two carriageways, a footway, a way split at every
        junction) — counting rows over-counts objects by a factor of three to six.
        To count bridges, buildings, stations, group by ``name`` (``bridge:name``)
        or by proximity first, then count the groups, and name them; say which of
        the two numbers you report.

        Optional ``where`` filters by attribute right after download, e.g.
        {"addr:street": "Hollerweg"} keeps only buildings on that street
        (case-insensitive exact match, every pair must hold). This avoids
        downloading a whole town and filtering afterwards.

        ``max_features`` defaults to None (keep every matched feature); pass
        an int only to cap a very large area for a quick map — the result's
        ``warning`` then flags that the count is incomplete.

        With ``place``, OSM returns every feature that *touches* the area,
        geometry uncut — a forest reaching into the city arrives whole. The
        result is therefore **clipped to the admin boundary** and the return
        value reports what that cost (``features_trimmed``,
        ``area_outside_km2``). Pass ``clip=false`` to keep whole features,
        e.g. to map a forest that continues past the city limit; then areas
        and counts are NOT those of the named area.
        """
        if not place and not bbox:
            return {"ok": False, "error": "provide either place or bbox"}
        try:
            import osmnx as ox

            output_path = resolve_path(output_path, ws, write=True)
            tags = _stringify_tags(tags)  # osmnx rejects int/float tag values
            clip_report: dict = {}
            if place:
                gdf = ox.features_from_place(place, tags=tags)
                if clip:
                    gdf, clip_report = clip_to_place(gdf, place)
            else:
                # The guard above made sure one of the two is set; without place it
                # is bbox.
                assert bbox is not None
                w, so, e, no = bbox
                gdf = ox.features_from_bbox((w, so, e, no), tags=tags)
            if gdf.empty:
                return {
                    "ok": False,
                    "error": f"no OSM features matched tags {tags}"
                    + _quoted_boolean_hint(tags),
                }

            if where:
                gdf, missing = _apply_where(gdf, where)
                if missing:
                    return {
                        "ok": False,
                        "error": f"where references unknown column(s) {missing}",
                        "available_columns": [c for c in gdf.columns if c != gdf.geometry.name][
                            :40
                        ],
                    }
                if gdf.empty:
                    return {"ok": False, "error": f"where {where} matched 0 features"}

            matched = len(gdf)
            # max_features=None (the default) downloads everything; a caller
            # may pass an int to cap large areas for a quick map.
            truncated = max_features is not None and matched > max_features
            if truncated:
                gdf = gdf.iloc[:max_features]
            _saveable(gdf).to_file(output_path)
            provenance.write_meta(
                output_path,
                source="connector/osm",
                tool="osm_features",
                query={
                    k: v
                    for k, v in {
                        "tags": tags,
                        "place": place,
                        "bbox": bbox,
                        "where": where,
                    }.items()
                    if v is not None
                },
                crs="EPSG:4326",
                licence=_OSM_LICENCE,
            )
        except Exception as exc:  # noqa: BLE001
            # osmnx raises InsufficientResponseError instead of returning an
            # empty frame when Overpass finds nothing, so the hint belongs on
            # both exits or it fires on neither.
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}" + _quoted_boolean_hint(tags),
            }
        geom_types = sorted({g.geom_type for g in gdf.geometry if g is not None})
        result = {
            "ok": True,
            "features": len(gdf),
            "geometry_types": geom_types,
            "crs": "EPSG:4326",
            "output": output_path,
        }
        result.update(clip_report)
        warnings: list[str] = []
        # Say the consequence where the layer is BORN. Measured 2026-09-05
        # (`supermarket-accessibility-choropleth`): the agent never called `vector_info`
        # in that flow, the note there never reached it — it learned of the mix from
        # `geometry_types` in exactly this return.
        mixed = mixed_geometry_note(geom_types)
        if mixed:
            result["mixed_geometry"] = True
            warnings.append(mixed)
        if place and clip:
            note = clip_warning(clip_report, place)
            if note:
                warnings.append(note)
        if clip_report.get("clip_error"):
            warnings.append(
                f"the boundary of {place} could not be looked up "
                f"({clip_report['clip_error']}), so features are NOT clipped: "
                "they may reach far beyond the named area. Verify before "
                "measuring areas or reporting a share."
            )
        if truncated:
            warnings.append(
                f"{matched} features matched but output was capped at "
                f"{max_features}; narrow the area/tags or raise max_features "
                "— the count is NOT complete."
            )
        if bbox and not place:
            warnings.append(
                "these features come from a BBOX (a rectangle), which includes "
                "neighbouring places — for a NAMED area (a city/Gemeinde/Kreis) this "
                "is an overcount and the wrong extent. If the task is about a named "
                'area, re-run with place="<name>" (that clips to the admin polygon), '
                "or vector_clip this layer against the boundary from "
                'geocode(query, output_path="boundary.gpkg"), before buffering/'
                "counting/mapping. Only keep the bbox result if an explicit "
                "coordinate window was intended."
            )
        or_note = _or_tags_warning(gdf, tags)
        if or_note:
            warnings.append(or_note.lstrip(" —").strip())
        if warnings:
            result["warning"] = " ".join(warnings)
        return result

    return [osm_features]
