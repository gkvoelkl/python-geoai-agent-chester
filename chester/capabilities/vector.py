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
from chester.capabilities.vectorops import op_tools
from chester.geo_python import hand_rolled_operations
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


#: Fingerabdruck der Abweisung, damit der nächste Aufruf sie zählen kann.
_GUARD_MARKER = "a checked function already does this"
_GUARD_MAX = 2


def _checked_route_guard(ctx, code: str) -> dict | None:
    """Weise einen Schnipsel **einmal** ab, der eine geprüfte Funktion nachbaut.

    Gemessen 2026-09-06 (`buffer-schools-500m`, QGIS abgeschaltet): Der Agent fand
    `geo_python_run` sofort — und schrieb darin dreimal rohes geopandas. Fachlich
    richtig, 84 Puffer in EPSG:25832, Fläche 784.137 m² gegen 785.398 m² Sollwert.
    Und jede Zusicherung lief ins Leere: `outputs: []`, `calls: []`, **kein einziger
    Provenienz-Sidecar**, keine Mixed-Geometry-Notiz. Der Notausgang war zur
    Hauptstraße geworden.

    **Einrundig**, aus der Geschichte des PyQGIS-Guards gelernt: Der zweite Aufruf
    desselben Schnipsels läuft. Es gibt Aufgaben, für die keine geprüfte Funktion
    existiert (ein Gini-Koeffizient, eine Kerndichte), und ein Riegel, der auch dann
    drängt, kostet nur Runden. Zusätzlich gedeckelt: nach `_GUARD_MAX` Abweisungen im
    Lauf schweigt er ganz.
    """
    hand_rolled = hand_rolled_operations(code or "")
    if not hand_rolled:
        return None
    if not getattr(ctx, "messages", None):
        return None  # Direktaufruf ohne Lauf — es gibt keine Runde zu zählen
    try:
        from selmakit import tool_returns

        # In Reihenfolge lesen: Was zählt, ist eine Abweisung **seit dem letzten
        # ausgeführten Schnipsel**. Würde ein einziges Nein den ganzen Lauf öffnen,
        # wäre der Riegel nach einer Runde wirkungslos — genau das passierte dem
        # PyQGIS-Guard am 2026-08-27 (eine Suche, danach zwölf handgeschriebene
        # Blöcke). Der Deckel darüber begrenzt, was er insgesamt beitragen kann.
        refused_since_run = False
        refusals_total = 0
        for name, content in tool_returns(ctx):
            if name != "geo_python_run":
                continue
            is_refusal = (isinstance(content, dict)
                          and _GUARD_MARKER in str(content.get("error", "")))
            if is_refusal:
                refused_since_run = True
                refusals_total += 1
            else:
                refused_since_run = False  # der Schnipsel lief — wieder scharf
    except Exception:  # noqa: BLE001 - unlesbarer Kontext darf die Arbeit nie blockieren
        return None
    if refused_since_run or refusals_total >= _GUARD_MAX:
        return None
    listed = "; ".join(f"`{name}` — {gain}" for name, gain in hand_rolled)
    return {
        "ok": False,
        "error": (
            f"{_GUARD_MARKER}: {listed}. These are **tools** — call them directly, "
            "one call per step, instead of writing a snippet. They take and return "
            "paths, report what went in and out, refuse metric work in a geographic "
            "CRS and stamp provenance; a hand-rolled equivalent returns "
            "`outputs: []`, writes no sidecar, and leaves the validation gate blind "
            "to what you produced. (Inside a snippet the same operations are bound "
            "under their short names — reproject, buffer, clip … — for when several "
            "steps in one go are cheaper.) Or call this again with the same code and "
            "it will run: for anything the tools do not cover, that is the right "
            "answer."
        ),
        "checked_functions": [name for name, _ in hand_rolled],
    }


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

        def geo_python_run(ctx: RunContext[Any], code: str,
                           timeout_seconds: int = 300) -> dict:
            """Run a GeoPandas snippet when no named tool fits.

            The namespace already holds `gpd`/`pd`/`np`, `shapely` (with `Point`,
            `Polygon`, `box`, …), `pyproj` and `rasterio` — imports are allowed but
            never needed. Assign a JSON-serialisable value to `result` to return it;
            `print(...)` is captured as `stdout`.

            Read and write through the two injected helpers rather than
            `gpd.read_file`/`to_file`: `read_vector(path)` collapses every path
            spelling and warns about a mixed-geometry layer before you compute on it,
            `write_vector(gdf, path)` puts the file in the GeoCache, stamps its
            provenance and reports it back. Both are recorded in `calls`.

            For a single standard operation prefer the named tools (`vector_filter`,
            `vector_overlay`, `vector_split_by_geometry`, `qgis_run` when QGIS is
            present) — they carry their own checks. This is the escape hatch for what
            none of them expresses.
            """
            from chester.geo_python import GeoPythonError, run_geo_python

            hand_rolled = _checked_route_guard(ctx, code)
            if hand_rolled:
                return hand_rolled
            cache_dir = Path(resolve_path("x.gpkg", ws)).parent
            cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                verdict = run_geo_python(code, cwd=str(cache_dir), timeout=timeout_seconds)
            except GeoPythonError as exc:
                return {"ok": False, "error": str(exc)}

            for path in verdict.get("outputs") or []:
                provenance.write_meta(
                    path, source="chester", tool="geo_python_run", query=code
                )
            if not verdict.get("ok"):
                return {"ok": False, "error": verdict.get("error") or "unknown error",
                        "stdout": verdict.get("stdout") or "",
                        "calls": verdict.get("calls") or []}
            return {
                "ok": True,
                "result": verdict.get("result"),
                "stdout": verdict.get("stdout") or "",
                "outputs": verdict.get("outputs") or [],
                # Die Aufrufe der geprüften Helfer stehen bewusst hier im **Inhalt**
                # der Rückgabe. `selmakit.tool_returns` liest `part.content` und
                # verwirft `part.metadata`; was ein Unterprozess tut, wäre dort sonst
                # unsichtbar und das Gate fiele lautlos aus — genau der Defekt, der am
                # 2026-09-06 an CodeMode gefunden wurde.
                "calls": verdict.get("calls") or [],
            }

        # Die zehn geprüften Operationen stehen in `vectorops.py` — dünne Hüllen
        # um `geoops`, ausgelagert, damit diese Datei ihre Baseline hält.
        return FunctionToolset(
            tools=[vector_info, vector_filter, vector_overlay,
                   vector_split_by_geometry, geo_python_run, *op_tools(ws)]
        )
