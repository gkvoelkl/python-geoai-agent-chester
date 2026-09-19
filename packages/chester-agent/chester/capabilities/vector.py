"""VectorCapability — in-memory vector inspection and analysis with GeoPandas.

Complements the QGIS tools: quick attribute/geometry questions and lightweight
overlays that don't warrant a full ``qgis_process`` round trip. geopandas is
imported lazily inside the tools to keep agent startup fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.runtime.geopython import build_geo_python_run
from chester.vectoroptools import build_tools as op_build_tools
from chester.vectortools import build_tools
from chester.workspace import DEFAULT_WORKSPACE

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





        # The escape hatch lives in chester-runtime (shared with chester-team).
        geo_python_run = build_geo_python_run(ws)

        # Die zehn geprüften Operationen stehen in `chester/vectoroptools.py` — dünne Hüllen
        # um `geoops`, ausgelagert, damit diese Datei ihre Baseline hält.
        return FunctionToolset(
            tools=[*build_tools(ws), geo_python_run, *op_build_tools(ws)]
        )
