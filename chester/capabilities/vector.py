"""VectorCapability — in-memory vector inspection and analysis with GeoPandas.

Complements the QGIS tools: quick attribute/geometry questions and lightweight
overlays that don't warrant a full ``qgis_process`` round trip. geopandas is
imported lazily inside the tools to keep agent startup fast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import provenance
from chester.geofacts import populated_columns as _populated_columns
from chester.geofacts import vector_facts
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

_OVERLAY_HOWS = {"intersection", "union", "difference", "symmetric_difference", "identity"}

_INSTRUCTIONS = """\
## Vector analysis (GeoPandas)

For inspecting and lightly transforming vector layers without QGIS:
- `vector_info` — geometry type, CRS, feature count, attribute columns, bounds.
  Lists only the *populated* columns (OSM layers carry hundreds of mostly-empty
  tag columns). Use it to learn a layer's schema before filtering.
- `vector_filter` — keep features matching a pandas attribute expression, e.g.
  "height > 15" or "type == 'residential'". Quote string literals with single
  quotes. Column names with special characters (e.g. OSM's `addr:street`) are
  backticked automatically, so "addr:street == 'Hollerweg'" just works.
- `vector_overlay` — geometric overlay of two layers (intersection, union,
  difference, …). Both layers should share a CRS.

Tip: to count/extract OSM features by an attribute (e.g. buildings on one
street), prefer `osm_features(..., where={"addr:street": "Hollerweg"})` — it
filters at download time and avoids the inspect-then-filter dance entirely.\
"""


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


@dataclass
class VectorCapability(AbstractCapability[Any]):
    """GeoPandas-backed vector tools (info, attribute filter, overlay)."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace

        def vector_info(path: str) -> dict:
            """Describe a vector layer: CRS, feature count, geometry types,
            attribute columns with dtypes, and bounding box.

            Only *populated* columns are listed (OSM exports carry hundreds of
            mostly-empty tag columns); ``columns_total`` / ``columns_empty``
            report how many were hidden.
            """
            try:
                f = vector_facts(resolve_path(path, ws), full=True)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            return {
                "ok": True,
                "features": f["feature_count"],
                "geometry_types": f["geometry_types"],
                "crs": f["crs"],
                "columns": f["columns"],
                "columns_total": f["columns_total"],
                "columns_empty": f["columns_empty"],
                "bounds": f["bounds"],
            }

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

                output_path = resolve_path(output_path, ws)
                gdf = gpd.read_file(resolve_path(path, ws))
                before = len(gdf)
                query = _backtick_special_columns(expression, gdf.columns)
                try:
                    filtered = gdf.query(query)
                except Exception as exc:  # noqa: BLE001 - guide the model to a fix
                    cols = _populated_columns(gdf)
                    return {
                        "ok": False,
                        "error": f"could not evaluate '{expression}': {type(exc).__name__}: {exc}",
                        "available_columns": cols[:40],
                        "hint": "Use single quotes for string values and backticks "
                        "for column names with ':' or '-', e.g. "
                        "\"`addr:street` == 'Hollerweg'\".",
                    }
                if filtered.empty:
                    return {
                        "ok": False,
                        "error": f"expression '{expression}' matched 0 of {before} features",
                        "before": before,
                        "after": 0,
                    }
                filtered.to_file(output_path)
                provenance.write_meta(
                    output_path, source="chester", tool="vector_filter",
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

        def vector_overlay(
            input_path: str, overlay_path: str, how: str, output_path: str
        ) -> dict:
            """Geometric overlay of two vector layers.

            how is one of: intersection, union, difference, symmetric_difference,
            identity. Both layers should be in the same CRS.
            """
            if how not in _OVERLAY_HOWS:
                return {"ok": False,
                        "error": f"unknown how '{how}'; "
                                 f"one of {sorted(_OVERLAY_HOWS)}"}
            try:
                import geopandas as gpd

                output_path = resolve_path(output_path, ws)
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
                    output_path, source="chester", tool="vector_overlay",
                    query={"how": how},
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {"ok": True, "features": len(result), "output": output_path}

        return FunctionToolset(tools=[vector_info, vector_filter, vector_overlay])
