"""The four vector tools as **framework-neutral** wrappers.

Phase KM, step 1. `vector_info` reads a layer's schema, `vector_filter` filters with a
pandas expression, `vector_overlay` overlays, and `vector_split_by_geometry` splits a
mixed layer by geometry type — the antidote to algorithms that silently keep one
type and drop the rest.

**What is missing here and why:** `geo_python_run` stays in the capability. Its lock
reads the run so far through `selmakit.tool_returns` to see whether a refusal came
since the last snippet — that is framework knowledge and does not belong in a pure
module. For Chester-MCP the tool is excluded anyway (`internal/chester-mcp.md` §3).
The instruction block stays there for the same reason: it describes both in one
piece.

The eleven checked operations live in `chester/vectoroptools.py`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from chester import provenance
from chester.geofacts import column_values, vector_facts
from chester.geofacts import populated_columns as _populated_columns
from chester.workspace import resolve_path

#: The vector group's instruction block — here, not in the capability, so the
#: ressorts of chester-team read the same text (2026-09-19).
INSTRUCTIONS = """\
## Vector analysis (GeoPandas)

For inspecting and lightly transforming vector layers without QGIS:
- `vector_info` — geometry type, CRS, feature count, attribute columns, bounds.
  Lists only the *populated* columns (OSM layers carry hundreds of mostly-empty
  tag columns). Use it to learn a layer's schema before filtering. To see what is
  actually *in* a column — which of 41 boundary features is the district you
  want — pass `values_of="name"`; it returns the distinct values, so picking the
  right feature never needs a PyQGIS snippet.
- `vector_filter` — keep features matching a pandas attribute expression, e.g.
  "height > 15" or "type == 'residential'". Quote string literals with single
  quotes. Column names with special characters (e.g. OSM's `addr:street`) are
  backticked automatically, so "addr:street == 'Hollerweg'" just works.
- `vector_overlay` — geometric overlay of two layers (intersection, union,
  difference, …). Both layers should share a CRS.
- `vector_split_by_geometry` — one layer holding several geometry types becomes
  one file per type, and **nothing else changes**: every feature keeps its exact
  type, its attributes and the CRS. Reach for it as soon as `vector_info` reports
  `mixed_geometry` (OSM maps larger shops as buildings, smaller ones as points),
  because an algorithm fed a mixed layer may keep ONE type and drop the rest
  without a word. If you only need to COUNT, replacing the areas with their
  centroids is the other route — but that REPLACES areas with points: usable for
  counting, wrong for anything measured.

- `geo_python_run` — the escape hatch: an arbitrary **GeoPandas** snippet, run in
  a subprocess with `gpd`/`pd`/`np`, `shapely`, `pyproj` and `rasterio` already
  bound. Use it when no named tool expresses the step (a custom statistic, a
  per-feature loop, a multi-step chain), not for a single standard operation.
  Read and write through the injected `read_vector(path)` / `write_vector(gdf,
  path)`: they collapse the path spellings, warn about a mixed-geometry layer
  before you compute on it, put outputs in the GeoCache with provenance, and report
  what they did in `calls`. Assign to `result` to return a value.
  The same ten operations are **tools**, and that is the normal way to use them —
  one call per step, no snippet needed: `vector_reproject`, `vector_buffer`,
  `vector_clip`, `vector_intersection`, `vector_extract_by_location`,
  `vector_extract_by_attribute`, `vector_dissolve`, `vector_merge`,
  `vector_add_field`, `vector_field_sum`. Inside a snippet they are bound as well, under their short
  names (`reproject`, `buffer`, `clip`, …), for when several steps in one go are
  cheaper than several tool calls. Call one when it fits and write by hand when it
  does not — in the same snippet. They refuse metric
  work in a geographic CRS, say when an output came back empty, and keep every
  geometry type a mixed layer holds. `vector_merge` is how split parts go back
  together: it aligns the CRSs first, where a hand-rolled `pd.concat` either fails
  on them or, for a layer with no CRS, adopts the neighbour's and leaves the
  coordinates where they were. The eleven raster/terrain/network operations
  (`rasterize`, `zonal_stats`, `slope`, `service_area`, …) are tools too — see their
  own section — and are bound here under the same names.

Tip: to count/extract OSM features by an attribute (e.g. buildings on one
street), prefer `osm_features(..., where={"addr:street": "Hollerweg"})` — it
filters at download time and avoids the inspect-then-filter dance entirely.\
"""

_OVERLAY_HOWS = {"intersection", "union", "difference", "symmetric_difference", "identity"}


_SQL_TELLS = (
    (re.compile(r"\bIN\s*\(", re.I), "`IN (…)` → `in ['a', 'b']` (square brackets)"),
    (re.compile(r"\bIS\s+(NOT\s+)?NULL\b", re.I), "`IS NULL` → `.isna()` does not work in query; "
                                                 "check empty values with vector_info first"),
    (re.compile(r"\bAND\b"), "`AND` → `and` (lower case)"),
    (re.compile(r"\bOR\b"), "`OR` → `or` (lower case)"),
    (re.compile(r'"[A-Za-z_][\w:]*"\s*(==|!=|<|>|\bin\b)', re.I),
     'column in double quotes → in pandas "x" is a string, not a column; '
     "columns stand bare (backticks only for `:` or `-`)"),
)


def _sql_syntax_hint(expression: str) -> str | None:
    """A hint at SQL syntax in the pandas expression — ``None`` if there is none.

    Names the concrete spots instead of a general rule, and the two tools that really
    accept SQL-like expressions. In this project the return channel is what turns
    behaviour; an instruction was already there.
    """
    found = [fix for pattern, fix in _SQL_TELLS if pattern.search(expression)]
    if not found:
        return None
    return (
        "This looks like SQL — `vector_filter` takes a **pandas** expression: "
        + "; ".join(found)
        + ". For a single field value `vector_extract_by_attribute` is simpler, "
        "for a real QGIS expression "
        "`qgis_run('native:extractbyexpression')`."
    )


def _backtick_special_columns(expression: str, columns) -> str:
    """Wrap column names that aren't valid Python identifiers in backticks.

    pandas ``DataFrame.query`` parses a bare ``addr:street`` as a Python
    annotation (the ``:``) and fails; backticked ``\\`addr:street\\``` is the
    documented escape. Small local models don't know this, so we do it for them.
    Already-backticked and identifier-safe names are left untouched.
    """
    for col in sorted(columns, key=len, reverse=True):  # longest first
        if col.isidentifier() or f"`{col}`" in expression:
            continue
        # Match the column only as a standalone token (not inside a longer name
        # or an already-backticked span).
        pattern = re.compile(r"(?<![\w`])" + re.escape(col) + r"(?![\w`])")
        expression = pattern.sub(f"`{col}`", expression)
    return expression


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """The four vector tools, bound to ``workspace``."""
    ws = workspace

    def vector_info(path: str, values_of: str | None = None) -> dict:
        """Describe a vector layer: CRS, feature count, geometry types,
        attribute columns with dtypes, and bounding box.

        Only *populated* columns are listed (OSM exports carry hundreds of
        mostly-empty tag columns); ``columns_total`` / ``columns_empty``
        report how many were hidden.

        Works on a **table without geometry** too (a CSV): ``kind`` then says
        ``"table"``, CRS and bounds are empty, and the column dtypes are the
        point — an AGS as ``int64`` on one side and as ``str`` on the other is
        the whole story of a join that silently matches nothing.

        Pass ``values_of="name"`` to also get that column's distinct values —
        the answer to "which of these features is the one I want?" (e.g. which
        of 41 boundary features is the district), so picking one needs no code.
        """
        resolved = resolve_path(path, ws)
        try:
            f = vector_facts(resolved, full=True)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        out = {
            "ok": True,
            # `kind` tells a vector layer from a table. A CSV has no CRS and no
            # extent; that is not a fault but the answer — and its column type
            # decides every join.
            "kind": f.get("kind", "vector"),
            "features": f["feature_count"],
            "geometry_types": f["geometry_types"],
            "crs": f["crs"],
            "columns": f["columns"],
            "columns_total": f["columns_total"],
            "columns_empty": f["columns_empty"],
            "bounds": f["bounds"],
        }
        # Several geometry families in one layer: `note` explains the consequence,
        # the flag is the machine-readable form of the same statement.
        if f.get("mixed_geometry"):
            out["mixed_geometry"] = True
        if f.get("note"):
            out["note"] = f["note"]
        if values_of:
            try:
                out["values"] = column_values(resolved, values_of)
            except Exception as exc:  # noqa: BLE001 - the layer facts still stand
                out["values"] = {"column": values_of, "error": f"{type(exc).__name__}: {exc}"}
        return out

    def vector_filter(path: str, expression: str, output_path: str) -> dict:
        """Keep only features matching a pandas query ``expression``.

        Example expressions: "height > 15", "landuse == 'forest'",
        "addr:street == 'Hollerweg'", "area_m2 >= 100 and floors < 5".
        Column names with special characters (OSM's ``addr:street``) are
        backticked automatically. Writes the filtered layer to output_path
        and returns how many features remained.
        """
        try:
            import geopandas as gpd

            output_path = resolve_path(output_path, ws, write=True)
            gdf = gpd.read_file(resolve_path(path, ws))
            before = len(gdf)
            query = _backtick_special_columns(expression, gdf.columns)
            try:
                filtered = gdf.query(query)
            except Exception as exc:  # noqa: BLE001 - guide the model to a fix
                out = {
                    "ok": False,
                    "error": f"could not evaluate '{expression}': {type(exc).__name__}: {exc}",
                    "hint": _sql_syntax_hint(expression)
                    or "Use single quotes for string values and backticks "
                    "for column names with ':' or '-', e.g. "
                    "\"`addr:street` == 'Hollerweg'\".",
                }
                # The column list only when the error looks like an unknown
                # column. For a syntax error it does not answer the question and
                # fills the context: in the triggering case 40 OSM attribute
                # names like `TMC:cid_58:tabcd_1:LocationCode`.
                if not isinstance(exc, SyntaxError):
                    out["available_columns"] = _populated_columns(gdf)[:40]
                return out
            if filtered.empty:
                return {
                    "ok": False,
                    "error": f"expression '{expression}' matched 0 of {before} features",
                    "before": before,
                    "after": 0,
                }
            filtered.to_file(output_path)
            provenance.write_meta(
                output_path,
                source="chester",
                tool="vector_filter",
                query=expression,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "before": before,
            "after": len(filtered),
            "output": output_path,
        }

    def vector_overlay(input_path: str, overlay_path: str, how: str, output_path: str) -> dict:
        """Geometric overlay of two vector layers.

        how is one of: intersection, union, difference, symmetric_difference,
        identity. Both layers should be in the same CRS.
        """
        if how not in _OVERLAY_HOWS:
            return {
                "ok": False,
                "error": f"unknown how '{how}'; one of {sorted(_OVERLAY_HOWS)}",
            }
        try:
            import geopandas as gpd

            output_path = resolve_path(output_path, ws, write=True)
            left = gpd.read_file(resolve_path(input_path, ws))
            right = gpd.read_file(resolve_path(overlay_path, ws))
            if left.crs != right.crs:
                return {
                    "ok": False,
                    "error": f"CRS mismatch: {left.crs} vs {right.crs}; reproject first",
                }
            result = gpd.overlay(left, right, how=how)
            if result.empty:
                return {"ok": False, "error": "overlay produced 0 features", "output": None}
            result.to_file(output_path)
            provenance.write_meta(
                output_path,
                source="chester",
                tool="vector_overlay",
                query={"how": how},
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "features": len(result), "output": output_path}

    def vector_split_by_geometry(path: str, output_prefix: str) -> dict:
        """Split a layer into one file per geometry type. Changes nothing else.

        Writes ``<output_prefix>_point.gpkg``, ``_multipolygon.gpkg`` and so on —
        only the types actually present — and returns the paths with their
        feature counts. Attributes, CRS and coordinates are carried over
        untouched, and **no geometry is converted**: a Point stays a Point, a
        Polygon stays a Polygon.

        Use it when one layer holds several geometry types and the next
        algorithm would silently keep only one of them.
        """
        try:
            import geopandas as gpd

            source = resolve_path(path, ws)
            gdf = gpd.read_file(source)
            if gdf.empty:
                return {"ok": False, "error": f"{Path(source).name} holds no features"}
            # Group by the **exact** type, not by family. Grouped by family, the
            # writer has to unify each group to one type and promotes single to
            # multi part: measured 2026-09-05, a `Point` became a `MultiPoint` and a
            # `Polygon` a `MultiPolygon`. A tool meant to split must not reshape
            # anything — otherwise it is a second `centroids`.
            types = sorted(gdf.geom_type.dropna().unique())
            if len(types) < 2:
                return {
                    "ok": False,
                    "error": (f"nothing to split — {Path(source).name} already holds "
                              f"one geometry type ({types[0] if types else 'none'}). "
                              "Use it as it is."),
                    "geometry_types": types,
                }
            parts = []
            for geom_type in types:
                # The target path goes through `resolve_path` like any other: that is
                # also the touch-on-read point that protects the file from pruning.
                out_path = resolve_path(
                    f"{output_prefix}_{geom_type.lower()}.gpkg", ws, write=True
                )
                part = gdf[gdf.geom_type == geom_type]
                part.to_file(out_path)
                provenance.write_meta(
                    out_path, source="chester", tool="vector_split_by_geometry",
                    query=f"{Path(source).name} → {geom_type}",
                )
                parts.append(
                    {"geometry_type": geom_type, "features": len(part), "output": out_path}
                )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "features": len(gdf),
            "parts": parts,
            # Why this tool computes in Python and not via `qgis_run`: measured
            # 2026-09-05, every QGIS algorithm that passes a mixed layer through
            # writes a header with only ONE type — a source correctly declared as
            # GEOMETRY became POINT, contents unchanged. A split via QGIS would
            # inherit exactly the defect it is built against.
            "note": ("nothing was converted — every feature keeps its exact geometry "
                     "type, attributes and CRS, and each part now carries a header "
                     "that matches its contents, so QGIS algorithms can no longer "
                     "drop features silently. Work on the part the task is about, or "
                     "on each in turn. (`native:centroids` is the other route, but it "
                     "REPLACES areas with points: fine for counting, wrong for "
                     "anything measured — area, distance, a buffer's reach.)"),
        }

    return [vector_info, vector_filter, vector_overlay, vector_split_by_geometry]
