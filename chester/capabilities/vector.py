"""VectorCapability — in-memory vector inspection and analysis with GeoPandas.

Complements the QGIS tools: quick attribute/geometry questions and lightweight
overlays that don't warrant a full ``qgis_process`` round trip. geopandas is
imported lazily inside the tools to keep agent startup fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import provenance
from chester.geo_python import hand_rolled_operations
from chester.vectoroptools import op_tools
from chester.vectortools import build_tools
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

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

        # Die zehn geprüften Operationen stehen in `chester/vectoroptools.py` — dünne Hüllen
        # um `geoops`, ausgelagert, damit diese Datei ihre Baseline hält.
        return FunctionToolset(
            tools=[*build_tools(ws), geo_python_run, *op_tools(ws)]
        )
