"""VectorCapability — in-memory vector inspection and analysis with GeoPandas.

Complements the QGIS tools: quick attribute/geometry questions and lightweight
overlays that don't warrant a full ``qgis_process`` round trip. geopandas is
imported lazily inside the tools to keep agent startup fast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import provenance
from chester.geofacts import column_values, vector_facts
from chester.geofacts import populated_columns as _populated_columns
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

_OVERLAY_HOWS = {"intersection", "union", "difference", "symmetric_difference", "identity"}

_INSTRUCTIONS = """\
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
  because a QGIS algorithm fed a mixed layer keeps ONE type and drops the rest
  without a word. `native:centroids` is the alternative, but it REPLACES areas
  with points: usable for counting, wrong for anything measured.

Tip: to count/extract OSM features by an attribute (e.g. buildings on one
street), prefer `osm_features(..., where={"addr:street": "Hollerweg"})` — it
filters at download time and avoids the inspect-then-filter dance entirely.\
"""


#: SQL-Merkmale, die in einem pandas-Ausdruck einen Syntaxfehler ergeben. Der Fall,
#: der diese Erkennung ausgelöst hat (2026-09-03, `laguna-xs-2.1` auf
#: `pluvial-flow-accumulation-tegernheim`): Das Modell schrieb
#: `"waterway" IN ('stream', …) AND geometry IS NOT NULL`, bekam einen SyntaxError
#: samt Hinweis auf Anführungszeichen und Backticks, befolgte den Hinweis, scheiterte
#: erneut — und wich danach auf handgeschriebenes PyQGIS aus. Der Hinweis war nicht
#: falsch, er war zum Fehler unpassend, und das kostete mehr als gar keiner.
#:
#: Dass gerade hier SQL getippt wird, ist hausgemacht: Der übrige Werkzeugkasten ist
#: QGIS- und SQL-geprägt (`qgis_extract_by_attribute`, `native:extractbyexpression`),
#: dieses eine Werkzeug spricht pandas.
_SQL_TELLS = (
    (re.compile(r"\bIN\s*\(", re.I), "`IN (…)` → `in ['a', 'b']` (eckige Klammern)"),
    (re.compile(r"\bIS\s+(NOT\s+)?NULL\b", re.I), "`IS NULL` → `.isna()` geht in query nicht; "
                                                 "leere Werte vorher mit vector_info prüfen"),
    (re.compile(r"\bAND\b"), "`AND` → `and` (klein)"),
    (re.compile(r"\bOR\b"), "`OR` → `or` (klein)"),
    (re.compile(r'"[A-Za-z_][\w:]*"\s*(==|!=|<|>|\bin\b)', re.I),
     'Spalte in doppelten Anführungszeichen → in pandas ist "x" ein Text, keine Spalte; '
     "Spalten stehen nackt da (Backticks nur bei `:` oder `-`)"),
)


def _sql_syntax_hint(expression: str) -> str | None:
    """Ein Hinweis auf SQL-Syntax im pandas-Ausdruck — ``None``, wenn keine da ist.

    Nennt die konkreten Stellen statt einer allgemeinen Regel, und die zwei
    Werkzeuge, die SQL-nahe Ausdrücke wirklich annehmen. Der Rückgabekanal ist in
    diesem Projekt der Weg, der Verhalten dreht; eine Instruktion war schon da.
    """
    found = [fix for pattern, fix in _SQL_TELLS if pattern.search(expression)]
    if not found:
        return None
    return (
        "Das sieht nach SQL aus — `vector_filter` nimmt einen **pandas**-Ausdruck: "
        + "; ".join(found)
        + ". Für einen einzelnen Feldwert ist `qgis_extract_by_attribute` "
        "einfacher, für einen echten QGIS-Ausdruck "
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
                # `kind` unterscheidet Vektorlayer von Tabelle. Eine CSV hat kein
                # CRS und keine Ausdehnung; das ist keine Störung, sondern die
                # Antwort — und ihr Spaltentyp entscheidet über jeden Join.
                "kind": f.get("kind", "vector"),
                "features": f["feature_count"],
                "geometry_types": f["geometry_types"],
                "crs": f["crs"],
                "columns": f["columns"],
                "columns_total": f["columns_total"],
                "columns_empty": f["columns_empty"],
                "bounds": f["bounds"],
            }
            # Mehrere Geometriefamilien in einer Ebene: `note` erklärt die Folge,
            # das Flag ist die maschinenlesbare Fassung derselben Aussage.
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

                output_path = resolve_path(output_path, ws)
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
                    # Die Spaltenliste nur, wenn der Fehler nach einer unbekannten
                    # Spalte aussieht. Bei einem Syntaxfehler beantwortet sie die
                    # Frage nicht und füllt den Kontext: im auslösenden Fall 40 OSM-
                    # Attributnamen wie `TMC:cid_58:tabcd_1:LocationCode`.
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
                # Nach dem **exakten** Typ gruppieren, nicht nach der Familie. Bei
                # Familien-Gruppierung muss der Schreiber innerhalb einer Gruppe auf
                # einen Typ vereinheitlichen und befördert Einzel- zu Mehrteil:
                # gemessen 2026-09-05 wurde aus einem `Point` ein `MultiPoint` und aus
                # einem `Polygon` ein `MultiPolygon`. Ein Werkzeug, das aufteilen soll,
                # darf nichts umformen — sonst ist es ein zweites `centroids`.
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
                    # Der Zielpfad geht durch `resolve_path` wie jeder andere: Das ist
                    # auch der Touch-on-read-Punkt, der die Datei vor dem Aufräumen schützt.
                    out_path = resolve_path(f"{output_prefix}_{geom_type.lower()}.gpkg", ws)
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
                # Der Grund, warum dieses Werkzeug in Python rechnet und nicht über
                # `qgis_run`: Gemessen 2026-09-05 schreibt jeder QGIS-Algorithmus, der
                # eine gemischte Ebene durchreicht, einen Kopf mit nur EINEM Typ — aus
                # einer korrekt als GEOMETRY deklarierten Quelle wurde POINT, bei
                # unverändertem Inhalt. Ein Split über QGIS erbte genau den Defekt,
                # gegen den er gebaut ist.
                "note": ("nothing was converted — every feature keeps its exact geometry "
                         "type, attributes and CRS, and each part now carries a header "
                         "that matches its contents, so QGIS algorithms can no longer "
                         "drop features silently. Work on the part the task is about, or "
                         "on each in turn. (`native:centroids` is the other route, but it "
                         "REPLACES areas with points: fine for counting, wrong for "
                         "anything measured — area, distance, a buffer's reach.)"),
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
                    output_path,
                    source="chester",
                    tool="vector_overlay",
                    query={"how": how},
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {"ok": True, "features": len(result), "output": output_path}

        return FunctionToolset(
            tools=[vector_info, vector_filter, vector_overlay,
                   vector_split_by_geometry]
        )
