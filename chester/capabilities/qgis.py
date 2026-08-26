"""QgisToolboxCapability — Chester's access to the whole QGIS toolbox.

Two layers of tools, both routed through the same :class:`QgisProcess` runner:

* **Generic (primary):** ``qgis_search`` / ``qgis_describe`` / ``qgis_run`` let
  the model discover any of QGIS's ~450 local algorithms at runtime and invoke it
  parameterized. This is the design's core lever — capabilities grow with every
  installed QGIS plugin.
* **Standard shortcuts:** 11 named, typed wrappers around the most common
  algorithms, so routine ops don't need a search→describe→run round trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import provenance
from chester.qgis_process import QgisProcess, QgisProcessError
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

# QGIS parameter names whose values are file paths (resolved to the workspace).
# LAYERS is list-valued (e.g. native:mergevectorlayers) — the rest are scalar
# strings; _resolve_params handles both shapes.
_PATH_KEYS = {
    "INPUT", "INPUT_2", "OUTPUT", "OVERLAY", "INTERSECT", "INPUT_RASTER",
    "INPUT_A", "INPUT_B", "INPUT_C", "INPUT_D", "INPUT_E", "INPUT_F",
    "DEM", "MASK", "FIELD_MAPPING", "LAYERS", "JOIN",
    # `native:countpointsinpolygon` names its two layers POINTS and POLYGONS.
    # Missing from this set, both arrived unresolved and qgis_process answered
    # "Could not load source layer for POLYGONS: … not found" — a path problem
    # wearing the words of a missing file. The model then hunted the file it had
    # just written, burning four turns on `list_directory` (2026-08-19,
    # `supermarket-accessibility-choropleth`).
    "POINTS", "POLYGONS", "LINES", "POLYGON", "HUBS", "SPOKES",
    # `native:rastersampling` calls its raster RASTERCOPY. Same trap as the pair
    # above, found the same way (2026-08-23, building `qgis_sample_raster`): the
    # path went through unresolved and QGIS answered "Could not load source layer
    # for RASTERCOPY: geocache/dem.tif not found" — a resolution failure wearing the
    # words of a missing file. Any new wrapper must check its parameter names
    # against this set first.
    "RASTERCOPY",
}

# Raster file extensions — qgis_reproject dispatches these to gdal:warpreproject
# (a raster warp) instead of the vector-only native:reprojectlayer.
_RASTER_EXTS = {
    ".tif", ".tiff", ".vrt", ".img", ".asc", ".jp2", ".grd", ".nc",
    ".hgt", ".dem", ".dt2", ".bil", ".jpg", ".png",
}


def _input_is_geographic(resolved_path: str) -> bool:
    """True if the vector layer is in a geographic (degrees) CRS.

    Cheap header read (no full load) so qgis_buffer can refuse a metric-distance
    buffer on a degree-CRS layer — where ``distance=500`` would mean 500° of arc,
    not 500 m. Unknown/no CRS → False (don't block).
    """
    try:
        import pyogrio
        from pyproj import CRS

        crs = pyogrio.read_info(resolved_path).get("crs")
        return bool(crs) and CRS.from_user_input(crs).is_geographic
    except Exception:  # noqa: BLE001 - can't tell → let the op proceed
        return False

# QGIS enum orderings (stable across versions).
_PREDICATE = {
    "intersects": 0,
    "contains": 1,
    "disjoint": 2,
    "equals": 3,
    "touches": 4,
    "overlaps": 5,
    "within": 6,
    "crosses": 7,
}
# native:extractbyattribute OPERATOR enum (stable QGIS ordering).
_ATTR_OPERATOR = {
    "=": 0, "==": 0,
    "!=": 1, "<>": 1,
    ">": 2,
    ">=": 3,
    "<": 4,
    "<=": 5,
    "begins with": 6, "startswith": 6,
    "contains": 7,
    "is null": 8,
    "is not null": 9,
    "does not contain": 10,
}
# Assumed travel speeds (km/h) per mode — used to turn a time budget into a
# network distance, so the service-area isochrone is computed with the
# unambiguous "shortest" (distance) strategy instead of QGIS's version-dependent
# time-cost unit.
_TRAVEL_SPEEDS_KMH = {"walk": 4.5, "bike": 15.0, "drive": 50.0}

# A service-area start point further than this (metres) from the network's
# extent is almost certainly a swapped or mis-projected coordinate (the classic
# lat,lon swap lands the point thousands of km away) — refused up front with a
# clear message instead of QGIS's cryptic "Point is too far from the network".
_MAX_START_DISTANCE_M = 50_000

# A minimal empty QGIS project, shipped in the repo. The network-analysis
# algorithms (serviceareafrompoint) refuse to run without a project context;
# this satisfies that with no layers/CRS of its own.
_EMPTY_PROJECT = str(Path(__file__).resolve().parent.parent / "resources" / "empty.qgs")

# native:fieldcalculator FIELD_TYPE enum (stable QGIS ordering). Verified against
# QGIS 4.2 by writing each type and reading the column back (2026-08-24).
_FIELD_TYPE = {"double": 0, "float": 0, "decimal": 0, "integer": 1, "int": 1,
               "string": 2, "text": 2, "date": 3}

_STATISTIC = {
    "count": 0,
    "sum": 1,
    "mean": 2,
    "median": 3,
    "stdev": 4,
    "min": 5,
    "max": 6,
    "range": 7,
    "minority": 8,
    "majority": 9,
    "variety": 10,
    "variance": 11,
}

_INSTRUCTIONS = """\
## QGIS toolbox

You run real GIS operations through QGIS. Two ways:

1. Generic path (works for ANY of ~450 algorithms): `qgis_search("slope")` to
   find an algorithm id, `qgis_describe("native:slope")` to learn its exact
   parameters, then `qgis_run("native:slope", {...})`.
2. Shortcuts for common ops: qgis_reproject, qgis_buffer, qgis_clip,
   qgis_intersection, qgis_extract_by_location, qgis_extract_by_attribute,
   qgis_dissolve, qgis_add_field, qgis_field_sum, qgis_sample_raster,
   qgis_service_area, qgis_zonal_stats, qgis_raster_calc. Use qgis_add_field to
   write ONE computed column ($area, num_points($geometry), a ratio of two
   fields) — do not build a field in PyQGIS, that API moved between QGIS 3
   and 4. Use qgis_sample_raster to write a raster's
   value at each POINT into a column (elevation at a viewpoint, land cover under a
   stop) — the point counterpart of qgis_zonal_stats, which needs polygons.
   Use qgis_extract_by_attribute to select features by a field value
   (e.g. field="addr:street", value="Hollerweg") — it needs no expression
   quoting and handles OSM colon field names directly. Use qgis_service_area for
   travel-time reach (isochrones / walkability) — real network distance, not a
   straight-line buffer.

Rules:
- Inputs and outputs are file paths. Write outputs into the workspace dir.
- To MEASURE a layer (total area, total length, or the sum of any numeric
  field), call qgis_field_sum in one step: qgis_field_sum(path, formula="$area")
  for total area, formula="$length" for total line length, or field="col" to
  total an existing column. Do NOT chain a field calculator with a statistics
  step by hand, and do NOT use qgis_zonal_stats for this (that is raster-only).
- CRS matters: before any area or distance calculation, make sure layers are in a
  projected (metric) CRS — reproject with qgis_reproject if needed. A wrong CRS
  gives a systematically wrong answer, not an approximate one.
- A tool result with `"ok": false` carries an `"error"` — read it and adjust the
  parameters, do not repeat the same call.\
"""


def _ok(results: dict) -> dict:
    return {"ok": True, **results}


def _err(exc: Exception) -> dict:
    return {"ok": False, "error": str(exc)}


def _measured(measured: str | None, stats: dict) -> dict:
    """A measurement result — with a warning when nothing was actually measured.

    ``count: 0`` comes back as ``sum: 0.0``, and a bare 0.0 reads like an answer:
    "the area is zero". It never is — it means the layer was empty, the column held
    no numbers, or the expression matched nothing. Observed 2026-08-22 (benchmark
    ``supermarkets-within-10min-walk``): a quoted ``"$area"`` measured a non-empty
    isochrone as 0.0, and the model spent the rest of its request budget trying to
    explain the zero. Saying what the number cannot do is the tool's job, not the
    model's.
    """
    out = {"ok": True, "measured": measured, **stats}
    if not stats.get("count"):
        out["warning"] = (
            f"nothing was measured — {measured!r} produced no numeric values over "
            "this layer, so the 0.0 is 'no data', not a result. Check that the layer "
            "holds features, that the field name exists and is numeric, and that a "
            "geometry expression is written unquoted ($area, $length)."
        )
    return out


# Reading every geometry costs ~8 ms for 250 features and ~20 ms for 340; above
# this many it is no longer a rounding error next to the algorithm itself, so the
# check steps aside rather than taxing a large run.
_GEOMETRY_CHECK_MAX_FEATURES = 200_000
# Where the algorithm's own layer sits — the one whose geometry the output is
# supposed to *be*. Deliberately only these: OVERLAY/MASK is a cutting shape, not
# data, and `POINTS` belongs to countpointsinpolygon, whose output is the POLYGONS
# layer. Including it made that algorithm accuse itself of losing its input, and
# whether the accusation appeared depended on QGIS promoting Polygon to
# MultiPolygon or not — a check with a coin flip in it is worse than none.
_INPUT_KEYS = ("INPUT", "LAYERS")


def _geometry_types(path: str) -> dict[str, int] | None:
    """The geometry types really present in a vector file, counted.

    Read, not asked. A GeoPackage header records **one** type — the writer's
    declaration — and a mixed layer therefore reports whatever came first:
    `supermarkets_25832.gpkg` announces `Point` while holding 109 points and 138
    polygons (measured 2026-08-19). Any check built on the metadata would have
    confirmed exactly the wrong thing.
    """
    import os

    if not isinstance(path, str) or not os.path.isfile(path):
        return None
    try:
        from pyogrio import read_dataframe, read_info

        if read_info(path).get("features", 0) > _GEOMETRY_CHECK_MAX_FEATURES:
            return None
        frame = read_dataframe(path, columns=[], read_geometry=True)
    except Exception:  # noqa: BLE001 - a diagnostic must never break the run
        return None
    return {str(k): int(v) for k, v in frame.geom_type.value_counts().items()}


def _primary_input(parameters: dict) -> str | None:
    for key in _INPUT_KEYS:
        value = parameters.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and value and isinstance(value[0], str):
            return value[0]
    return None


def _sole_output(results: dict) -> str | None:
    outputs = [v for v in (results.get("results") or {}).values() if isinstance(v, str)]
    return outputs[0] if len(outputs) == 1 else None


# Algorithms whose output geometry type is **constructed**, not inherited: a buffer
# is always polygons, centroids always points, whatever went in. They "lose" every
# input type by design, so the dropped-geometry check must not look at them at all.
# Deciding this from the *data* was not enough (2026-08-23, `buffer-schools-500m`):
# the guard below skips the check when the output holds a type the input lacked —
# but the 84 schools happened to include one MultiPolygon, so a buffer to
# MultiPolygon looked type-preserving and the warning fired on a perfectly good
# result. It claimed "25× Point, 58× Polygon dropped … missing those features
# entirely" when nothing was missing. The agent believed it, converted the schools
# to centroids and re-buffered — an 11,3 % smaller catchment (29,867 → 26,492 km²),
# which the judge then praised. A false warning is worse than no warning.
_TYPE_CONSTRUCTING_ALGORITHMS = frozenset({
    "native:buffer",
    "native:centroids",
    "native:pointonsurface",
    "native:convexhull",
    "native:concavehull",
    "native:boundary",
    "native:countpointsinpolygon",
    "native:polygonstolines",
    "native:linestopolygons",
    "native:pointstopath",
    "native:minimumboundinggeometry",
    "native:voronoipolygons",
    "native:delaunaytriangulation",
    "native:extractvertices",
    "native:pointsalonglines",
    "qgis:linestopolygons",
})


def _dropped_geometry_warning(
    parameters: dict, results: dict, algorithm_id: str | None = None
) -> str | None:
    """Name the geometry types an algorithm silently threw away.

    `native:clip` (and its siblings) write **one** geometry type. Handed a mixed
    layer they keep the first and drop the rest without a word: 247 supermarkets
    in, 107 out, the 138 polygon-mapped stores gone — and since larger shops are
    the ones drawn as buildings, the survivors were the small ones. The run that
    found this reported 18 supermarkets for a district that has 80 (2026-08-19,
    `supermarket-accessibility-choropleth`). Every call said `ok: true`.
    """
    if (algorithm_id or results.get("id")) in _TYPE_CONSTRUCTING_ALGORITHMS:
        return None
    source = _primary_input(parameters)
    target = _sole_output(results)
    if not source or not target or source == target:
        return None
    before = _geometry_types(source)
    after = _geometry_types(target)
    if not before or not after or len(before) < 2:
        return None
    # Only when the algorithm *kept* the input's geometry kind. `buffer` turns
    # points into polygons, `centroids` polygons into points, and
    # `countpointsinpolygon` returns the POLYGONS layer entirely — all of them
    # "lose" an input type by design, and warning about that would be noise that
    # teaches the model to ignore the field. Measured: without this guard the
    # count-in-polygon run below warned about its own correct result.
    if any(k not in before for k in after):
        return None
    lost = {k: n for k, n in before.items() if k not in after}
    if not lost:
        return None
    listed = ", ".join(f"{n}× {k}" for k, n in sorted(lost.items()))
    return (
        f"this algorithm writes ONE geometry type, and the input held several — "
        f"{listed} was dropped, not clipped away. The result is missing those "
        f"features entirely. If they matter (OSM maps larger shops and buildings "
        f"as polygons, smaller ones as points), convert the input to a single type "
        f"first — native:centroids turns polygons into countable points — and rerun. "
        f"Only when you COUNT or SELECT: a centroid stands in for a shape, so "
        f"anything you MEASURE from it (area, distance, a buffer's reach) comes out "
        f"too small. Measure on the shapes themselves."
    )


@dataclass
class QgisToolboxCapability(AbstractCapability[Any]):
    """Exposes QGIS algorithms as LLM tools via the ``qgis_process`` CLI."""

    workspace: str = DEFAULT_WORKSPACE
    timeout: int = 600

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        qp = QgisProcess(timeout=self.timeout)
        ws = self.workspace

        def _resolve_one(value: str) -> str:
            """Resolve one path, preserving a QGIS ``|layername=…`` qualifier."""
            source, sep, qualifier = value.partition("|")
            return resolve_path(source, ws) + sep + qualifier

        def _resolve_params(parameters: dict) -> dict:
            resolved = {}
            for key, value in parameters.items():
                if key not in _PATH_KEYS:
                    resolved[key] = value
                elif isinstance(value, str):
                    resolved[key] = _resolve_one(value)
                elif isinstance(value, list):
                    # multi-layer params (LAYERS) carry a list of path sources —
                    # resolve each string element, leave non-strings untouched.
                    resolved[key] = [
                        _resolve_one(v) if isinstance(v, str) else v for v in value
                    ]
                else:
                    resolved[key] = value
            return resolved

        _qp_run = qp.run

        def _record_outputs(algorithm_id: str, parameters: dict, results: dict) -> None:
            """Stamp a 'chester' provenance sidecar on each output file produced.

            One chokepoint for all 9 wrappers + the generic ``qgis_run``: the
            tool is the algorithm id, the query is the non-path parameters (the
            operation that made the layer, e.g. the buffer distance or formula).
            """
            import os

            query = {
                k: v for k, v in parameters.items()
                if k not in _PATH_KEYS and k != "OUTPUT"
            } or None
            # qp.run returns {"id", "results": {OUTPUT: path, ...}, "inputs": {...}};
            # the produced files are the values of the nested "results" dict.
            for value in (results.get("results") or {}).values():
                if isinstance(value, str) and os.path.isfile(value):
                    provenance.write_meta(
                        value, source="chester", tool=algorithm_id, query=query
                    )

        def _run(algorithm_id: str, parameters: dict) -> dict:
            """Run after resolving path-valued parameters into the workspace.

            The same chokepoint that stamps provenance also answers "did this
            quietly lose data?" — a QGIS algorithm reports success either way, and
            a silently dropped geometry type has cost a whole benchmark run.
            """
            resolved = _resolve_params(parameters)
            results = _qp_run(algorithm_id, resolved)
            _record_outputs(algorithm_id, resolved, results)
            warning = _dropped_geometry_warning(resolved, results, algorithm_id)
            if warning:
                results = {**results, "warning": warning}
            return results

        # ── generic meta-tools ──────────────────────────────────────────

        def qgis_search(keyword: str) -> list[dict]:
            """Search the QGIS toolbox for algorithms matching a keyword.

            Matches the id, name, description and tags (e.g. 'buffer', 'slope',
            'zonal statistics'). Returns up to 25 {id, name, group, provider,
            description} entries. Use this first when no shortcut tool fits.
            """
            try:
                return qp.search(keyword)
            except QgisProcessError as exc:
                return [_err(exc)]

        def qgis_describe(algorithm_id: str) -> dict:
            """Return the exact parameters and outputs of one QGIS algorithm.

            Call this before qgis_run for an unfamiliar algorithm to get the
            precise parameter names, types, defaults and which are required.
            """
            try:
                return qp.describe(algorithm_id)
            except QgisProcessError as exc:
                return _err(exc)

        def qgis_run(algorithm_id: str, parameters: dict) -> dict:
            """Run any QGIS algorithm with a parameters dict.

            ``parameters`` maps QGIS parameter names to values, e.g.
            {"INPUT": "in.gpkg", "DISTANCE": 50, "OUTPUT": "out.gpkg"}. Paths are
            files on disk; put outputs in the workspace. Returns the output paths.
            """
            try:
                return _ok(_run(algorithm_id, parameters))
            except QgisProcessError as exc:
                return _err(exc)

        # ── 8 standard shortcuts ────────────────────────────────────────

        def qgis_reproject(input_path: str, target_crs: str, output_path: str) -> dict:
            """Reproject a vector or raster layer to a target CRS (e.g. 'EPSG:25832').

            Do this before measuring areas/distances — or computing slope/terrain
            on a DEM — if the layer is in a geographic CRS like EPSG:4326. Rasters
            (.tif, …) are warped with ``gdal:warpreproject``; vectors use
            ``native:reprojectlayer``. The right algorithm is chosen from the
            input's file type, so the same call works for both.
            """
            import os

            is_raster = os.path.splitext(input_path)[1].lower() in _RASTER_EXTS
            algorithm = "gdal:warpreproject" if is_raster else "native:reprojectlayer"
            try:
                out = _ok(
                    _run(
                        algorithm,
                        {"INPUT": input_path, "TARGET_CRS": target_crs, "OUTPUT": output_path},
                    )
                )
            except QgisProcessError as exc:
                return _err(exc)
            try:
                from pyproj import CRS

                # The EPSG code alone leaves the answer to guess the name, and it
                # guesses wrong: 25832 was reported to the user as "Gauß-Krüger"
                # (2026-08-26) when it is ETRS89 / UTM zone 32N.
                out["target_crs_name"] = CRS.from_user_input(target_crs).name
            except Exception:  # noqa: BLE001 — a missing name must not fail the reprojection
                pass
            return out

        def qgis_buffer(
            input_path: str, distance: float, output_path: str, dissolve: bool = False
        ) -> dict:
            """Buffer features by ``distance`` **metres** (the layer must be metric).

            Set dissolve=True to merge overlapping buffers into one feature.

            Refuses a layer in a geographic CRS (EPSG:4326, degrees): there a
            ``distance`` of 500 would be 500° of arc, not 500 m. Reproject to a
            metric CRS first (``qgis_reproject`` → EPSG:25832), then buffer.
            """
            if _input_is_geographic(resolve_path(input_path, ws)):
                return {
                    "ok": False,
                    "error": f"input is in a geographic CRS (degrees) — a "
                    f"{distance} buffer would be measured in DEGREES, not metres. "
                    "Reproject to a metric CRS first (qgis_reproject to "
                    "EPSG:25832), then buffer.",
                }
            try:
                return _ok(
                    _run(
                        "native:buffer",
                        {
                            "INPUT": input_path,
                            "DISTANCE": distance,
                            "DISSOLVE": dissolve,
                            "OUTPUT": output_path,
                        },
                    )
                )
            except QgisProcessError as exc:
                return _err(exc)

        def qgis_clip(input_path: str, overlay_path: str, output_path: str) -> dict:
            """Clip ``input_path`` features to the **geometry** of ``overlay_path``.

            This clips to the overlay polygon itself, not its bounding box — so
            holes are honoured. Clipping an OSM-by-bbox download to a geocoded
            admin boundary therefore both drops features in neighbouring areas
            (outside the polygon) AND excludes any enclave that is a hole in the
            boundary — e.g. a kreisfreie Stadt sitting inside a Landkreis ring.
            The overlay must be in the same CRS as the input.
            """
            try:
                return _ok(
                    _run(
                        "native:clip",
                        {"INPUT": input_path, "OVERLAY": overlay_path, "OUTPUT": output_path},
                    )
                )
            except QgisProcessError as exc:
                return _err(exc)

        def qgis_intersection(input_path: str, overlay_path: str, output_path: str) -> dict:
            """Geometric intersection of two vector layers (keeps overlap + attributes)."""
            try:
                return _ok(
                    _run(
                        "native:intersection",
                        {"INPUT": input_path, "OVERLAY": overlay_path, "OUTPUT": output_path},
                    )
                )
            except QgisProcessError as exc:
                return _err(exc)

        def qgis_extract_by_location(
            input_path: str,
            reference_path: str,
            output_path: str,
            predicate: str = "intersects",
        ) -> dict:
            """Extract features of ``input_path`` by spatial relation to ``reference_path``.

            predicate is one of: intersects, contains, disjoint, equals, touches,
            overlaps, within, crosses.
            """
            code = _PREDICATE.get(predicate.lower())
            if code is None:
                return {"ok": False,
                        "error": f"unknown predicate '{predicate}'; "
                                 f"one of {list(_PREDICATE)}"}
            try:
                return _ok(
                    _run(
                        "native:extractbylocation",
                        {
                            "INPUT": input_path,
                            "INTERSECT": reference_path,
                            "PREDICATE": [code],
                            "OUTPUT": output_path,
                        },
                    )
                )
            except QgisProcessError as exc:
                return _err(exc)

        def qgis_extract_by_attribute(
            input_path: str,
            field: str,
            value: str,
            output_path: str,
            operator: str = "=",
        ) -> dict:
            """Extract features of ``input_path`` whose attribute ``field`` matches ``value``.

            Field-based selection: ``field`` is passed as a parameter, not parsed
            as an expression, so OSM-style names with colons work directly
            (field="addr:street", value="Hollerweg"). operator is one of:
            =, !=, >, >=, <, <=, "begins with", contains, "is null",
            "is not null", "does not contain". For the null operators ``value``
            is ignored.
            """
            code = _ATTR_OPERATOR.get(operator.lower().strip())
            if code is None:
                return {"ok": False,
                        "error": f"unknown operator '{operator}'; "
                                 f"one of {list(_ATTR_OPERATOR)}"}
            try:
                return _ok(
                    _run(
                        "native:extractbyattribute",
                        {
                            "INPUT": input_path,
                            "FIELD": field,
                            "OPERATOR": code,
                            "VALUE": value,
                            "OUTPUT": output_path,
                        },
                    )
                )
            except QgisProcessError as exc:
                return _err(exc)

        def qgis_dissolve(input_path: str, output_path: str, field: str | None = None) -> dict:
            """Dissolve (merge) geometries, optionally grouped by an attribute ``field``."""
            try:
                return _ok(
                    _run(
                        "native:dissolve",
                        {
                            "INPUT": input_path,
                            "FIELD": [field] if field else [],
                            "OUTPUT": output_path,
                        },
                    )
                )
            except QgisProcessError as exc:
                return _err(exc)

        def qgis_add_field(
            input_path: str,
            output_path: str,
            name: str,
            expression: str,
            field_type: str = "double",
        ) -> dict:
            """Add ONE computed column to a layer and write the result.

            ``expression`` is a QGIS expression over the layer's own fields and
            geometry — ``$area``, ``$length``, ``num_nodes($geometry)``,
            ``"pop" / "area_km2"``, ``round("h", 1)``. ``field_type`` is one of
            double (default) / integer / string / date.

            Reach for this instead of writing a snippet: creating a field in PyQGIS
            means `QgsField`, a Qt type enum and `QgsVectorFileWriter`, and that API
            moved between QGIS 3 and 4 — where most training data still lives. Three
            runs lost four to eleven calls each to exactly that (2026-08-23
            `gtfs-stops-departures-map-regensburg` and `viewpoints-above-400m`,
            2026-08-24 `mean-building-vertices`, which spent four calls and seven
            minutes on `QgsVectorFileWriter.SaveOptions` — an attribute QGIS 4 no
            longer has — for a value it had already computed).

            To *aggregate* instead of annotate (one number over the whole layer),
            use ``qgis_field_sum``; this writes one value per feature.

            One expression trap worth knowing: ``num_points($geometry)`` counts the
            **stored** vertices, and a closed ring stores its first point twice — so
            a rectangle counts 5, not 4. For *corners* subtract one per ring:
            ``num_points($geometry) - num_rings($geometry)``. Measured on the 1791
            Altstadt buildings (2026-08-24): 8.38 stored vertices per building
            against 7.36 corners. Both are right; say which you counted.
            """
            code = _FIELD_TYPE.get(field_type.strip().lower())
            if code is None:
                return {"ok": False,
                        "error": f"unknown field_type {field_type!r}; "
                                 f"choose from {sorted(set(_FIELD_TYPE))}"}
            try:
                return _ok(
                    _run(
                        "native:fieldcalculator",
                        {
                            "INPUT": input_path,
                            "FIELD_NAME": name,
                            "FIELD_TYPE": code,
                            "FIELD_LENGTH": 0,
                            "FIELD_PRECISION": 0,
                            "FORMULA": expression,
                            "OUTPUT": output_path,
                        },
                    )
                )
            except QgisProcessError as exc:
                return _err(exc)

        def qgis_sample_raster(
            points_path: str,
            raster_path: str,
            output_path: str,
            prefix: str = "value_",
        ) -> dict:
            """Write the raster's value at each POINT into a new column.

            The point counterpart of ``qgis_zonal_stats`` (which needs polygons):
            elevation at a viewpoint, land cover under a stop, NDVI at a sample
            site. Both layers must already share a CRS — sampling is a lookup, not a
            measurement, so a geographic CRS is fine as long as **both** are in it.
            The new column is ``<prefix>1`` for band 1 (``value_1``); filter on it
            afterwards with ``vector_filter``.

            Reach for this before writing a snippet: a run that hand-rolled it spent
            **eleven** consecutive ``qgis_python`` calls on field construction and
            still needed nineteen calls for a three-call task (2026-08-23,
            `viewpoints-above-400m`).
            """
            try:
                return _ok(
                    _run(
                        "native:rastersampling",
                        {
                            "INPUT": points_path,
                            "RASTERCOPY": raster_path,
                            "COLUMN_PREFIX": prefix,
                            "OUTPUT": output_path,
                        },
                    )
                )
            except QgisProcessError as exc:
                return _err(exc)

        def qgis_zonal_stats(
            zones_path: str,
            raster_path: str,
            output_path: str,
            statistics: list[str] | None = None,
            band: int = 1,
            prefix: str = "_",
        ) -> dict:
            """Compute raster statistics per polygon zone.

            statistics is a list from: count, sum, mean, median, stdev, min, max,
            range, minority, majority, variety, variance (default ['mean']). Result
            columns are named ``<prefix><stat>``.
            """
            stats = statistics or ["mean"]
            codes = [_STATISTIC[s.lower()] for s in stats if s.lower() in _STATISTIC]
            if not codes:
                return {"ok": False,
                        "error": f"no valid statistics in {stats}; "
                                 f"choose from {list(_STATISTIC)}"}
            try:
                return _ok(
                    _run(
                        "native:zonalstatisticsfb",
                        {
                            "INPUT": zones_path,
                            "INPUT_RASTER": raster_path,
                            "RASTER_BAND": band,
                            "COLUMN_PREFIX": prefix,
                            "STATISTICS": codes,
                            "OUTPUT": output_path,
                        },
                    )
                )
            except QgisProcessError as exc:
                return _err(exc)

        def qgis_field_sum(
            input_path: str,
            field: str | None = None,
            formula: str | None = None,
        ) -> dict:
            """Sum a numeric field (or an expression) over all features.

            The one-call way to *measure* a layer — e.g. total area or length —
            without adding a per-feature column and aggregating by hand. Give
            exactly one of:

            * ``field`` — an existing numeric column to total, or
            * ``formula`` — a QGIS expression; use ``"$area"`` for the total
              polygon area (in the layer's CRS units) or ``"$length"`` for total
              line length.

            For an area/length total the layer must be in a projected (metric)
            CRS — otherwise ``$area`` is measured in square degrees, not m².
            Returns sum/count/mean/min/max in a single pass; writes no output
            layer.
            """
            import os
            import tempfile

            from chester import geofacts

            if bool(field) == bool(formula):
                return {
                    "ok": False,
                    "error": "give exactly one of field or formula "
                    "(e.g. formula='$area' for total area)",
                }
            src = resolve_path(input_path, ws)
            if formula and _input_is_geographic(src) and any(
                g in formula.lower() for g in ("$area", "$length", "$perimeter")
            ):
                return {
                    "ok": False,
                    "error": "input is in a geographic CRS (degrees) — "
                    f"{formula} would be measured in degrees, not metres. "
                    "Reproject to a metric CRS first (qgis_reproject to "
                    "EPSG:25832), then sum.",
                }

            # Fast path: a field/$area/$length total is an in-process geopandas
            # read (seconds), not two qgis_process passes over the whole layer
            # (which time out at 100k+ features). Falls through to QGIS only for
            # an arbitrary expression measure_layer can't evaluate.
            try:
                fast = geofacts.measure_layer(src, field=field, formula=formula)
            except KeyError:
                return {"ok": False,
                        "error": f"field '{field}' not found in the layer"}
            except Exception:  # noqa: BLE001 — fall back to QGIS on any read issue
                fast = None
            if fast is not None:
                return _measured(formula or field, fast)

            tmp: str | None = None
            try:
                stat_field = field
                if formula:
                    # $area/$length has no column — materialise one into a temp
                    # layer (outside the cache), then aggregate it.
                    fd, tmp = tempfile.mkstemp(suffix=".gpkg")
                    os.close(fd)
                    os.remove(tmp)  # let QGIS create the file itself
                    fc = _qp_run(
                        "native:fieldcalculator",
                        {
                            "INPUT": src,
                            "FIELD_NAME": "_val",
                            "FIELD_TYPE": 0,  # decimal (double)
                            "FORMULA": formula,
                            "OUTPUT": tmp,
                        },
                    )
                    src = (fc.get("results") or {}).get("OUTPUT") or tmp
                    stat_field = "_val"

                stats = _qp_run(
                    "native:basicstatisticsforfields",
                    {"INPUT_LAYER": src, "FIELD_NAME": stat_field},
                )
                res = stats.get("results") or stats
            except QgisProcessError as exc:
                return _err(exc)
            finally:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)

            return _measured(
                formula or field,
                {
                    "sum": res.get("SUM"),
                    "count": res.get("COUNT"),
                    "mean": res.get("MEAN"),
                    "min": res.get("MIN"),
                    "max": res.get("MAX"),
                },
            )

        def qgis_service_area(
            network_path: str,
            start_lon: float,
            start_lat: float,
            minutes: float,
            output_path: str,
            mode: str = "walk",
            start_crs: str = "EPSG:4326",
        ) -> dict:
            """Network travel-time **isochrone**: the area reachable from a start
            point within ``minutes`` along ``network_path``, on foot by default.

            This is real network reach, not a straight-line buffer — the honest
            basis for accessibility / walkability ("what's reachable in 15 minutes
            on foot"). It runs QGIS ``serviceareafrompoint`` on the network, then
            wraps the reachable nodes in a concave hull to return **one isochrone
            polygon**.

            - ``network_path`` — a LINE layer (e.g. OSM roads/paths) in a projected
              (metric) CRS. Reproject first (``qgis_reproject`` → EPSG:25832).
            - ``start_lon`` / ``start_lat`` — the origin coordinates, **longitude
              then latitude**, in WGS84 (the ``start_crs`` default). Pass a
              ``geocode`` centroid straight through: it is ``[lon, lat]``, so
              ``start_lon = centroid[0]`` and ``start_lat = centroid[1]``. They are
              named separately precisely so the order can't be swapped; QGIS
              transforms the point to the network CRS.
            - ``start_crs`` — the CRS of ``start_lon``/``start_lat``. Leave it at
              ``EPSG:4326`` for geographic (geocoded) coordinates; set it to the
              network's own CRS to pass a projected easting/northing directly (then
              ``start_lon``/``start_lat`` are x/y in that CRS).
            - ``minutes`` — the time budget.
            - ``mode`` — walk / bike / drive; sets the assumed speed (4.5 / 15 / 50
              km/h). The budget is converted to a network distance, so the result is
              deterministic and CRS-unit-safe (no reliance on QGIS's time-cost unit).
            """
            import os
            import tempfile

            speed = _TRAVEL_SPEEDS_KMH.get(mode)
            if speed is None:
                return {"ok": False, "error": f"unknown mode '{mode}'; one of "
                        f"{list(_TRAVEL_SPEEDS_KMH)}"}
            net_resolved = resolve_path(network_path, ws)
            if _input_is_geographic(net_resolved):
                return {
                    "ok": False,
                    "error": "network is in a geographic CRS (degrees) — reproject "
                    "it to a metric CRS first (qgis_reproject to EPSG:25832), so a "
                    "travel distance is measured in metres.",
                }
            distance_m = (minutes / 60.0) * speed * 1000.0
            # Named lon/lat + explicit CRS → QGIS's "x,y [EPSG:…]" START_POINT
            # form, so QGIS transforms the origin to the network CRS itself. The
            # order can't be swapped because the parameters are named.
            start_point = f"{start_lon},{start_lat} [{start_crs}]"

            # Guardrail: project the start into the network CRS ourselves and
            # reject it if it lands far outside the network extent — the tell-tale
            # of a swapped/mis-projected coordinate — with an actionable message,
            # rather than letting QGIS fail with "Point is too far from the network
            # layer (…)". Best-effort: a bounds/transform hiccup never blocks a run.
            try:
                import math

                from pyproj import Transformer

                from chester import geofacts

                nf = geofacts.vector_facts(net_resolved)
                nb, ncrs = nf.get("bounds"), nf.get("crs")
                if nb and ncrs:
                    sx, sy = Transformer.from_crs(
                        start_crs, ncrs, always_xy=True
                    ).transform(start_lon, start_lat)
                    gap_m = math.hypot(
                        max(nb[0] - sx, 0.0, sx - nb[2]),
                        max(nb[1] - sy, 0.0, sy - nb[3]),
                    )
                    if not math.isfinite(gap_m) or gap_m > _MAX_START_DISTANCE_M:
                        wb = nf.get("bounds_wgs84")
                        extent = (
                            f" The network covers lon {wb[0]}–{wb[2]}, "
                            f"lat {wb[1]}–{wb[3]}." if wb else ""
                        )
                        how_far = (
                            f"{round(gap_m / 1000)} km" if math.isfinite(gap_m)
                            else "an implausible distance"
                        )
                        return {
                            "ok": False,
                            "error": (
                                f"start point is {how_far} from the "
                                "network — almost certainly swapped or mis-projected "
                                "coordinates. start_lon/start_lat are longitude then "
                                f"latitude in {start_crs}; for a geocode centroid pass "
                                "start_lon=centroid[0], start_lat=centroid[1]." + extent
                            ),
                        }
            except Exception:  # noqa: BLE001 - guardrail is advisory, never fatal
                pass

            def _mktemp() -> str:
                fd, path = tempfile.mkstemp(suffix=".gpkg")
                os.close(fd)
                os.remove(path)  # let QGIS create the file itself
                return path

            tmp_sa = tmp_pts = None
            try:
                tmp_sa, tmp_pts = _mktemp(), _mktemp()
                sa = _qp_run(
                    "native:serviceareafrompoint",
                    {
                        "INPUT": net_resolved,
                        "START_POINT": start_point,
                        "STRATEGY": 0,          # shortest = distance (metric, unambiguous)
                        "TRAVEL_COST2": distance_m,
                        "POINT_TOLERANCE": 500,  # snap the origin to the nearest edge
                        "INCLUDE_BOUNDS": True,
                        "OUTPUT": tmp_sa,
                    },
                    project_path=_EMPTY_PROJECT,  # network analysis needs a project
                )
                # Service area emits reachable nodes as MultiPoint features; the
                # concave hull needs individual points, so explode first.
                sa_out = (sa.get("results") or {}).get("OUTPUT") or tmp_sa
                exploded = _qp_run(
                    "native:multiparttosingleparts", {"INPUT": sa_out, "OUTPUT": tmp_pts}
                )
                points = (exploded.get("results") or {}).get("OUTPUT") or tmp_pts
                try:
                    import pyogrio

                    reachable = int(pyogrio.read_info(points).get("features") or 0)
                except Exception:  # noqa: BLE001 - guard is advisory
                    reachable = None
                if reachable is not None and reachable < 3:
                    return {
                        "ok": False,
                        "error": f"only {reachable} network nodes reachable — the "
                        "start point is probably off the network or the area is "
                        "unreachable. Check that start_lon/start_lat name a point "
                        "near a road (default WGS84 lon,lat; set start_crs if they "
                        "are projected), and that the network is connected.",
                    }
                out = _run(
                    "native:concavehull",
                    {
                        "INPUT": points,
                        "ALPHA": 0.3,
                        "HOLES": False,
                        "NO_MULTIGEOMETRY": True,
                        "OUTPUT": output_path,
                    },
                )
            except QgisProcessError as exc:
                return _err(exc)
            finally:
                for t in (tmp_sa, tmp_pts):
                    if t and os.path.exists(t):
                        os.remove(t)

            return _ok({**out, "mode": mode, "minutes": minutes,
                        "reach_distance_m": round(distance_m)})

        def qgis_raster_calc(
            input_a: str,
            formula: str,
            output_path: str,
            input_b: str | None = None,
            band_a: int = 1,
            band_b: int = 1,
        ) -> dict:
            """Raster algebra over one or two rasters using GDAL.

            Reference the rasters as A (and B) in ``formula``, e.g. 'A-B' to compute
            building height from a DSM (input_a) minus a DTM (input_b).
            """
            params: dict[str, Any] = {
                "INPUT_A": input_a,
                "BAND_A": band_a,
                "FORMULA": formula,
                "OUTPUT": output_path,
            }
            if input_b:
                params["INPUT_B"] = input_b
                params["BAND_B"] = band_b
            try:
                return _ok(_run("gdal:rastercalculator", params))
            except QgisProcessError as exc:
                return _err(exc)

        return FunctionToolset(
            tools=[
                qgis_search,
                qgis_describe,
                qgis_run,
                qgis_reproject,
                qgis_buffer,
                qgis_clip,
                qgis_intersection,
                qgis_extract_by_location,
                qgis_extract_by_attribute,
                qgis_dissolve,
                qgis_add_field,
                qgis_field_sum,
                qgis_sample_raster,
                qgis_service_area,
                qgis_zonal_stats,
                qgis_raster_calc,
            ]
        )
