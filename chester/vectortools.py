"""Die vier Vektor-Werkzeuge als **rahmenneutrale** Hüllen.

Phase KM, Schritt 1. `vector_info` liest das Schema einer Ebene, `vector_filter`
filtert über einen pandas-Ausdruck, `vector_overlay` verschneidet, und
`vector_split_by_geometry` trennt eine gemischte Ebene nach Geometrietyp — das
Gegenmittel gegen Algorithmen, die stillschweigend einen Typ behalten und den Rest
verwerfen.

**Was hier fehlt und warum:** `geo_python_run` bleibt in der Capability. Sein Riegel
liest mit `selmakit.tool_returns` den bisherigen Lauf, um zu sehen, ob seit dem letzten
Schnipsel eine Abweisung kam — das ist Rahmenwissen und gehört nicht in ein reines
Modul. Für Chester-MCP ist das Werkzeug ohnehin ausgeschlossen
(`internal/chester-mcp.md` §3). Der Instruktionsblock bleibt aus demselben Grund dort:
Er beschreibt beides in einem Stück.

Die elf geprüften Operationen stehen in `chester/vectoroptools.py`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from chester import provenance
from chester.geofacts import column_values, vector_facts
from chester.geofacts import populated_columns as _populated_columns
from chester.workspace import resolve_path

_OVERLAY_HOWS = {"intersection", "union", "difference", "symmetric_difference", "identity"}


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


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """Die vier Vektor-Werkzeuge, an ``workspace`` gebunden."""
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

    return [vector_info, vector_filter, vector_overlay, vector_split_by_geometry]
