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
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import provenance
from chester.qgis_process import QgisProcess, QgisProcessError
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

# QGIS parameter *types* whose values are file paths. Read off the algorithm's own
# schema (`qgis_process help <alg> --json`), which is why this set — unlike the name
# list below — does not have to grow with every new algorithm. Verified 2026-09-03
# against 17 algorithms across the native/gdal/grass providers; the types that are
# NOT paths and must stay out are Boolean, String, Number, Enum, Vector Field,
# Extent, Raster Band, CRS, Distance and Map Theme.
_PATH_TYPES = frozenset({
    "Raster Layer", "Vector Layer", "Vector Features", "Multiple Input",
    "Raster Destination", "Vector Destination", "Feature Sink",
    # Not seen in the sample above, but the same QGIS destination family; a file
    # path either way, and leaving them out would only reinstate the old gap.
    "File Destination", "Folder Destination",
})

#: The write half of `_PATH_TYPES` — a parameter of one of these types means the
#: caller asked for a file to be produced, so an empty result set is a failure.
_DESTINATION_TYPES = frozenset({
    "Raster Destination", "Vector Destination", "Feature Sink",
    "File Destination", "Folder Destination",
})

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

_INSTRUCTIONS = """\
## QGIS toolbox

QGIS is here for the **specialised** algorithms (~760, GRASS included): find one with
`qgis_search("slope")`, learn its exact parameters with `qgis_describe("native:slope")`,
then `qgis_run("native:slope", {...})`.

For the common operations use the named tools instead — `vector_reproject`,
`vector_buffer`, `vector_clip`, `vector_intersection`, `vector_extract_by_location`,
`vector_extract_by_attribute`, `vector_dissolve`, `vector_add_field`,
`vector_field_sum`, `rasterize`, `sample_raster`, `zonal_stats`, `raster_calc`,
`service_area`. They carry the checks a raw algorithm call does not (metric CRS,
empty results, mixed geometry, refusals with a reason). Reach for `qgis_run` only
when none of them fits.

Rules:
- Inputs and outputs are file paths. Write outputs into the workspace dir.
- A tool result with `"ok": false` carries an `"error"` — read it and adjust the
  parameters, do not repeat the same call.
- **Terrain hydrology: fill the sinks BEFORE you compute flow.** A raw DTM is full
  of pits, and flow paths computed on it end in those artefacts instead of running
  through. Order: `native:fillsinkswangliu` first, then feed its
  `OUTPUT_FILLED_DEM` — not the original raster — into the accumulation step
  (`grass:r.flow`, `grass:r.watershed`). Doing it the other way round produces a
  plausible-looking result that is wrong everywhere a depression sits.\
"""


def _ok(results: dict) -> dict:
    return {"ok": True, **results}


def _err(exc: Exception) -> dict:
    return {"ok": False, "error": str(exc)}


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
    # Eine **Tabelle ohne Geometrie** (CSV/XLSX) kommt als reiner DataFrame zurück,
    # und `.geom_type` gibt es dort nicht. Bis 2026-09-01 stand dieser Zugriff
    # außerhalb des try — `qgis_run("native:createpointslayerfromtable", …)` warf
    # deshalb einen AttributeError statt eines Ergebnisses, und das ist genau der
    # Aufruf, mit dem man aus geokodierten Adressen eine Punktebene macht. Der
    # Kommentar oben („a diagnostic must never break the run") galt für alles außer
    # der letzten Zeile.
    geom_type = getattr(frame, "geom_type", None)
    if geom_type is None:
        return None
    return {str(k): int(v) for k, v in geom_type.value_counts().items()}


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
        f"this algorithm writes ONE geometry type — the one the input file's header "
        f"DECLARES, which for a mixed layer is whatever the writer put there — and "
        f"the input held several: "
        f"{listed} was dropped, not clipped away. The result is missing those "
        f"features entirely. If they matter (OSM maps larger shops and buildings "
        f"as polygons, smaller ones as points), convert the input to a single type "
        f"first — native:centroids turns polygons into countable points — and rerun. "
        f"Only when you COUNT or SELECT: a centroid stands in for a shape, so "
        f"anything you MEASURE from it (area, distance, a buffer's reach) comes out "
        f"too small. Measure on the shapes themselves."
    )


#: "I did nothing" is not an error in QGIS — it is a number in the result. Per entry:
#: the counter of work done → the counter of what was left over, and the advice that
#: fits that particular kind of nothing. Deliberately short: each line stands for one
#: observed silent success, not for a catalogue of every algorithm.
_WORK_COUNTERS: dict[str, tuple[str, str]] = {
    "JOINED_COUNT": (
        "UNJOINABLE_COUNT",
        "A join matches on value AND type: the integer 9375117 is not the text "
        "\"09375117\". Every German AGS carries a leading zero in Bavaria (state key "
        "09), and reading the key as a number drops it. Compare both key columns with "
        "`vector_info` first — it reports the dtype — and align them before rerunning "
        "(native:fieldcalculator, e.g. lpad(to_string(\"ags\"), 8, '0')).",
    ),
}


def _did_nothing_warning(results: dict) -> str | None:
    """Say it when an algorithm reports that it changed nothing.

    Measured 2026-09-05 (`join-leading-zero-ags`, Test-Level 2):
    `native:joinattributestable` came back ``{"JOINED_COUNT": 0,
    "UNJOINABLE_COUNT": 4}`` and `qgis_run` passed that on as ``ok: true``. The
    output file existed, held all four municipality polygons, and carried the joined
    column — empty in every row. The agent read "ok", ticked the step off and moved
    on; only the probe's `no_nulls` check caught it, and real usage has no such
    check. This is the fourth silent success of the same family (unknown parameter
    name, empty `results`, returned path with no file) and the most treacherous,
    because the artefact looks entirely normal when opened.

    Not ``ok: false``: the algorithm *did* run, and calling that a failure would be
    as dishonest as the success. The result says what happened and what to check.
    """
    produced = results.get("results") or {}
    for counter, (leftover_key, advice) in _WORK_COUNTERS.items():
        did, left = produced.get(counter), produced.get(leftover_key)
        if did != 0 or not isinstance(left, int) or left <= 0:
            continue
        return (
            f"the algorithm ran, but {counter} is 0 of {left}: it matched NOTHING. "
            f"The output file exists and carries the input's geometry, yet every "
            f"field this step was supposed to fill is NULL. Do not report this as a "
            f"result. {advice}"
        )
    return None


def _declared_geometry_type(path: str) -> str | None:
    """What a vector file **claims** to hold, as opposed to what it holds.

    The GeoPackage header names exactly one geometry type. `_geometry_types` reads
    the geometries themselves; this reads the declaration. The gap between the two
    is the defect below.
    """
    import os

    if not isinstance(path, str) or not os.path.isfile(path):
        return None
    try:
        from pyogrio import read_info

        declared = read_info(path).get("geometry_type")
    except Exception:  # noqa: BLE001 - a diagnostic must never break the run
        return None
    return str(declared) if declared else None


#: The GeoPackage supertype: "any geometry type may occur here". Spelled `GEOMETRY`
#: in the spec's own table, `wkbUnknown` (0) in OGR, `"Unknown"` by pyogrio and
#: `QgsWkbTypes.Unknown` by QGIS — one value, four names, and the one that reads like
#: a defect is the honest one. `osm_features` writes it correctly for a layer holding
#: both shop points and shop buildings.
_GENERIC_DECLARATIONS = frozenset({"unknown", "geometry", "geometrycollection"})


def _mistyped_layer(path: str) -> tuple[str, dict[str, int]] | None:
    """``(declared, actual)`` when the header makes a claim the file does not keep.

    The line runs between an **honest** and a **dishonest** header, not between
    single-type and mixed layers. A mixed layer can be declared correctly — that is
    what `GEOMETRY` is for — and GDAL says so itself when it is asked to break the
    rule: *"a geometry of type POLYGON is inserted into layer of geometry type
    POINT, which is not normally allowed by the GeoPackage specification, but the
    driver will however do it."* Files like that are not merely awkward, they are
    non-conformant, and this project has two of them, both written by QGIS.

    An earlier version of this function excluded every mixed layer, on the premise
    that GeoPackage cannot describe one. That premise was wrong (2026-09-05), and it
    exempted the very step where the corruption happens: `native:reprojectlayer`
    turned an honest `GEOMETRY` header into `POINT` while keeping all 138 polygons.
    """
    actual = _geometry_types(path)
    declared = _declared_geometry_type(path)
    if not actual or not declared:
        return None
    if declared.lower() in _GENERIC_DECLARATIONS:
        return None  # "any type may occur" — true of every content
    if _families({declared: 1}) >= _families(actual):
        return None  # the declaration covers every family present
    return declared, actual


def _type_declaration_warning(
    parameters: dict, results: dict, algorithm_id: str | None = None
) -> str | None:
    """Say it when a layer's header disagrees with its contents.

    **The cause behind two of this project's longest-standing silent failures**,
    found 2026-09-05 by bisecting `supermarket-accessibility-choropleth`.
    `native:extractbyexpression` filtered a mixed layer down to 127 polygons but
    inherited the source's declaration — `POINT`, because two points sat among 319
    features. QGIS reports `wkbType() == 1` for that file while pyogrio reads 117
    Polygon + 10 MultiPolygon out of it. Every downstream algorithm believes the
    header, aims its output sink at points, matches nothing and writes a valid empty
    file with `ok: true`. Rewriting the identical 127 features with a correct
    declaration made the same clip return 58.

    It also reframes the 2026-08-19 finding: the 138 supermarket polygons were not
    lost because "clip writes one geometry type" — `supermarkets_25832.gpkg`
    declares `POINT`, so only the 108 points ever reached the clip, of which 18 lay
    inside. `_dropped_geometry_warning` names the right symptom with the wrong cause.

    Chester had both halves of the answer all along: `_geometry_types` reads the
    real types (built for exactly this untrustworthy header) and `vector_info`
    prints them. It simply never compared them against the declaration.

    The **input** is checked first: a mistyped input explains the empty result this
    step just produced, while a mistyped output is a trap for the next one.
    """
    for path, role in ((_primary_input(parameters), "input"),
                       (_sole_output(results), "output")):
        if not path:
            continue
        found = _mistyped_layer(path)
        if not found:
            continue
        declared, actual = found
        held = ", ".join(f"{n}× {k}" for k, n in sorted(actual.items()))
        where = ("The layer you fed in" if role == "input"
                 else "The layer this step just wrote")
        return (
            f"{Path(path).name} DECLARES `{declared}` in its header but holds "
            f"{held}. {where} is internally inconsistent, and QGIS algorithms trust "
            f"the header: they aim the output at `{declared}`, match none of the "
            f"other geometries and write a valid EMPTY file while reporting success. "
            f"Do not chain another algorithm onto it. Rewrite it with a declaration "
            f"that fits — filter to one family first (native:extractbyexpression "
            f"with geometry_type($geometry), or native:centroids to turn polygons "
            f"into countable points) and write that to a NEW file, then continue "
            f"from there. `vector_info` reads the real geometries, so use it to "
            f"check, not the header."
        )
    return None


def _empty_result_warning(
    parameters: dict, results: dict, algorithm_id: str | None = None
) -> str | None:
    """Say it when features went in and nothing came out.

    The most general silent success of them all, and the one that cost the longest
    run. Measured 2026-09-05 (`supermarket-accessibility-choropleth`): `native:clip`
    turned 127 municipality polygons into **0**, `native:countpointsinpolygon`
    counted into that empty layer, and `native:intersection` intersected it again —
    three consecutive `ok: true` returns over nothing, each writing a valid, empty
    GeoPackage. None of the existing checks could see it: `_dropped_geometry_warning`
    compares which types survived and needs a non-empty result to compare against,
    and `_did_nothing_warning` reads work counters that `clip` does not have.

    No algorithm-specific knowledge is needed for this one — an empty output from a
    non-empty input means the step did nothing, whatever the algorithm was.

    ``_geometry_types`` separates the two cases this depends on: ``{}`` is an empty
    *vector*, ``None`` is "not a readable vector" (a raster, a missing file). Only
    the first may warn, or every vector→raster algorithm would accuse itself.
    """
    source = _primary_input(parameters)
    target = _sole_output(results)
    if not source or not target or source == target:
        return None
    after = _geometry_types(target)
    if after != {}:  # something came out, or the output is not a vector at all
        return None
    before = _geometry_types(source)
    if not before:  # the input was empty too — then nothing is the honest answer
        return None
    went_in = sum(before.values())
    return (
        f"the output is EMPTY: {went_in} feature(s) went in, 0 came out, and the "
        f"file was written anyway. `ok: true` means a file exists, not that the step "
        f"worked — do not build on this layer and do not report it as a result. Two "
        f"causes account for nearly all of these: the layers do not actually overlap "
        f"(compare both CRS and the `vector_info` bounds — a boundary from a "
        f"different area, or a degree-based layer against a metric one, yields "
        f"exactly this), or the overlay geometry is invalid (native:fixgeometries on "
        f"it, then retry). Check with vector_info before the next step."
    )


def _families(counted: dict[str, int] | None) -> set[str]:
    """Die gezählten Typen auf point/line/polygon eindampfen — eine Tabelle, in geofacts."""
    from chester.geofacts import GEOMETRY_FAMILY

    return {GEOMETRY_FAMILY[k] for k in counted or {} if k in GEOMETRY_FAMILY}


def _swapped_geometry_warning(
    parameters: dict, results: dict, algorithm_id: str | None = None
) -> str | None:
    """Say it when an intersection returns the OVERLAY's shapes, not the input's.

    `native:intersection` is geometric, not selective: polygons ∩ points **is**
    points. The attributes of both layers survive, so the result reads exactly
    like the wanted one — same four buildings, same OSM tags — while the geometry
    has quietly become the address points.

    Measured 2026-09-01 (`map-then-geotiff`, Schritt 1): asked for the *footprints*
    of four Regensburg addresses, the agent downloaded the buildings, intersected
    them with the geocoded points, and rendered the result. The map showed four
    circles. Every call returned `ok: true`, the feature count was right (4), and
    the layer even carried `building=yes` — nothing in any return value said that
    the polygons were gone.
    """
    if (algorithm_id or results.get("id")) != "native:intersection":
        return None
    source, overlay = _primary_input(parameters), parameters.get("OVERLAY")
    target = _sole_output(results)
    if not source or not target or not isinstance(overlay, str):
        return None
    before, after = _families(_geometry_types(source)), _families(_geometry_types(target))
    over = _families(_geometry_types(overlay))
    if not before or not after or not over or after & before or after != over:
        return None
    lost, kept = ", ".join(sorted(before)), ", ".join(sorted(after))
    return (
        f"the geometry in this result is the OVERLAY's, not the input's: {lost} ∩ "
        f"{kept} is {kept}, so you now hold the input's ATTRIBUTES on the overlay's "
        f"SHAPES. Drawn on a map that is a {kept}, not a {lost} — an outline you "
        f"asked for would be missing. If you wanted the {lost} features that meet "
        f"the overlay, this is the wrong algorithm: vector_extract_by_location keeps "
        f"whole features (predicate 'intersects' or 'contains'). Use intersection "
        f"only when you really want the cut piece."
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
        # **Träge** aufgelöst. Bis zum 2026-09-06 stand hier `QgisProcess(...)`
        # direkt, und damit warf schon das *Bauen* des Werkzeugsatzes, sobald QGIS
        # fehlte oder über `geodata.use_qgis: false` abgeschaltet war — obwohl die
        # meisten Werkzeuge ihre Argumente prüfen, lange bevor sie QGIS anfassen.
        # Vierzehn Tests, die reine Argumentvalidierung prüfen (Grad-CRS ablehnen,
        # unbekannter Modus), fielen deshalb im QGIS-losen Modus aus. Jetzt entsteht
        # der Prozess beim ersten echten Aufruf; ein kaputtes oder abgeschaltetes
        # QGIS kostet damit auch keinen Startabbruch mehr.
        _qp_cache: list[QgisProcess] = []

        def _qp() -> QgisProcess:
            if not _qp_cache:
                _qp_cache.append(QgisProcess(timeout=self.timeout))
            return _qp_cache[0]

        class _LazyQgisProcess:
            """Reicht jeden Zugriff an den erst bei Bedarf gebauten Prozess weiter."""

            def __getattr__(self, name: str) -> Any:
                return getattr(_qp(), name)

        qp = _LazyQgisProcess()
        ws = self.workspace

        def _resolve_one(value: str) -> str:
            """Resolve one path, preserving a QGIS ``|layername=…`` qualifier."""
            source, sep, qualifier = value.partition("|")
            return resolve_path(source, ws) + sep + qualifier

        @lru_cache(maxsize=256)
        def _schema(algorithm_id: str) -> dict | None:
            """The algorithm's parameter/output schema, or None when unavailable.

            One describe per algorithm per process (it costs 1.7-2.9 s and cannot
            change within a run), shared by the three guards below.
            """
            try:
                return qp.describe(algorithm_id)
            except QgisProcessError:
                return None

        def _reject_unknown_parameters(algorithm_id: str, parameters: dict) -> None:
            """Refuse a parameter name the algorithm does not have.

            QGIS ignores unknown parameters **silently**. Measured 2026-09-04 in a
            live run: the model called `native:fillsinkswangliu` with `OUTPUT` —
            that algorithm has `OUTPUT_FILLED_DEM` instead — and qgis_process
            answered `ok: true` with `results: {}`, having written nothing. The next
            step failed on the file that never appeared, and the recovery took five
            minutes and ended on a *leftover* raster from an earlier run, computed
            without complaint. A silent success is the worst failure mode there is,
            so it is turned into a message that names the valid parameters.
            """
            schema = _schema(algorithm_id)
            if schema is None:
                return  # no schema, no opinion
            known = set(schema.get("parameters") or {})
            if not known:
                return
            unknown = sorted(set(parameters) - known)
            if not unknown:
                return
            outputs = sorted(schema.get("outputs") or {})
            hint = f" Its outputs are: {', '.join(outputs)}." if outputs else ""
            raise QgisProcessError(
                f"{algorithm_id} has no parameter(s): {', '.join(unknown)}.{hint} "
                f"Valid parameters: {', '.join(sorted(known))}. "
                "QGIS ignores unknown parameters silently, so this call would have "
                "reported success and written nothing."
            )

        def _require_declared_outputs(
            algorithm_id: str, parameters: dict, results: dict
        ) -> None:
            """An algorithm asked for a destination must return one.

            The backstop to the guard above: whatever the reason, if a destination
            parameter was supplied and `results` came back empty, no file exists and
            `ok: true` would be a lie the validation gate cannot catch.
            """
            schema = _schema(algorithm_id)
            if schema is None or results.get("results"):
                return
            specs = schema.get("parameters") or {}
            asked = sorted(
                name for name in parameters
                if str((specs.get(name) or {}).get("type") or "") in _DESTINATION_TYPES
            )
            if asked:
                raise QgisProcessError(
                    f"{algorithm_id} produced no output although "
                    f"{', '.join(asked)} was requested — nothing was written. "
                    "Check the parameter names against qgis_describe."
                )

        def _verify_outputs_exist(algorithm_id: str, results: dict) -> None:
            """A returned output path that is not on disk is a failure, not a success.

            The third and last way a QGIS call can lie about having produced
            something — and the one that hides best, because the answer *looks*
            complete: `ok: true` plus a plausible path. Measured 2026-09-04:
            `gdal:rastercalculator` reported success with
            `OUTPUT: .../tegernheim_sinks.tif` **three times** and never wrote the
            file. Cause: `A-B` over two rasters that do not share a grid (4038x5489
            against 4017x5491, bounds offset by ~14 m) — GDAL exits 0 and writes
            nothing. The model then read the missing file as a *path* problem and
            spent fifteen minutes probing `os.getcwd()` and `resolve_path`, which is
            the wrong road entirely.

            `_record_outputs` has always tested `os.path.isfile` before stamping
            provenance; it just skipped what was missing instead of saying so.

            This is the "physical state check" from GeoAgentBench's PEA metric
            (arXiv 2604.13888): for a path-valued parameter, verify the file exists
            in the sandbox. It needs no ground truth and it is deterministic.
            """
            import os

            missing = [
                value
                for value in (results.get("results") or {}).values()
                if isinstance(value, str)
                and (os.sep in value)
                and Path(value).suffix
                and not os.path.exists(value)
            ]
            if not missing:
                return
            raise QgisProcessError(
                f"{algorithm_id} reported success but did not write: "
                f"{', '.join(missing)}. This is NOT a path problem — that is where "
                "the file would be. The algorithm ran and produced nothing. A common "
                "cause is inputs that do not share a grid: gdal:rastercalculator "
                "writes nothing when A and B differ in extent or resolution. Check "
                "the inputs with vector_info and align them (vector_clip / "
                "gdal:warpreproject) before retrying."
            )

        @lru_cache(maxsize=256)
        def _schema_path_params(algorithm_id: str) -> frozenset[str]:
            """Path-valued parameter names, read from the algorithm's own schema.

            `_PATH_KEYS` below is a hand-kept list of names, and its own comment
            records two runs lost to a name that was missing from it. GRASS makes
            that approach untenable rather than merely risky: all 307 of its
            algorithms name their parameters in lower case (`elevation`,
            `accumulation`, `stream`), so no amount of adding names catches the
            next one. Measured 2026-09-03 in a live dialogue: `grass:r.flow` and
            `native:fillsinkswangliu` (whose output is `OUTPUT_FILLED_DEM`, also
            absent) wrote their results into the **repo root**, `qgis_run` reported
            `ok: true` with bare filenames, and the model then spent roughly twenty
            requests hunting files that were never where it looked.

            QGIS answers the question itself: `qgis_process help <alg> --json`
            types every parameter. Cached per algorithm — the call costs 1.7-2.9 s
            and the schema cannot change within a run.
            """
            schema = _schema(algorithm_id)
            if schema is None:
                # No schema (bad id, QGIS gone) — the caller still has _PATH_KEYS.
                return frozenset()
            return frozenset(
                name
                for name, spec in (schema.get("parameters") or {}).items()
                if str(spec.get("type") or "") in _PATH_TYPES
            )

        def _resolve_params(parameters: dict, algorithm_id: str | None = None) -> dict:
            # Union, not replacement: the schema is authoritative but optional, so a
            # describe that fails must not silently stop resolving `INPUT`/`OUTPUT`.
            keys = _PATH_KEYS | (
                _schema_path_params(algorithm_id) if algorithm_id else frozenset()
            )
            resolved = {}
            for key, value in parameters.items():
                if key not in keys:
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
                if k not in _PATH_KEYS
                and k not in _schema_path_params(algorithm_id)
                and k != "OUTPUT"
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
            _reject_unknown_parameters(algorithm_id, parameters)
            resolved = _resolve_params(parameters, algorithm_id)
            results = _qp_run(algorithm_id, resolved)
            _require_declared_outputs(algorithm_id, parameters, results)
            _verify_outputs_exist(algorithm_id, results)
            _record_outputs(algorithm_id, resolved, results)
            # Ursache vor Symptom: Eine falsche Typdeklaration erklärt die leere
            # Ausgabe, die die nächste Prüfung nur feststellen würde.
            warning = (_type_declaration_warning(resolved, results, algorithm_id)
                       or _did_nothing_warning(results)
                       or _empty_result_warning(resolved, results, algorithm_id)
                       or _dropped_geometry_warning(resolved, results, algorithm_id)
                       or _swapped_geometry_warning(resolved, results, algorithm_id))
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

        return FunctionToolset(
            tools=[qgis_search, qgis_describe, qgis_run]
        )
