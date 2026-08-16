"""GeoValidationCapability — the mandatory correctness phase of the loop.

Geodata results are objectively right or wrong. Before Chester
reports a result, it should check that the CRS is appropriate and that the output
is plausible. These tools make that checkable rather than vibes-based.

geopandas/rasterio are imported lazily inside the tools so the agent still boots
fast and so a missing raster stack doesn't break vector workflows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import plausibility
from chester.geofacts import (
    attribute_facts,
    compare_layers,
    dangle_facts,
    is_raster,
    measure_layer,
    raster_facts,
    topology_facts,
    vector_facts,
)
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

_INSTRUCTIONS = """\
## Validation (do this before reporting a result)

Geodata is right or wrong, not "roughly right". Before you present a final answer:
- Call `check_crs` on inputs you will measure. Area/distance need a *projected*
  (metric) CRS; if a layer is geographic (degrees, e.g. EPSG:4326), reproject it
  first with qgis_reproject.
- Call `sanity_check_result` on your output. If it reports `ok: false` or
  warnings (empty result, invalid geometries, suspicious counts), investigate and
  fix the step instead of reporting a wrong answer.
- For a themed result, give `sanity_check_result` your expectations so it can check
  the attributes too: `required=[...]` (fields that must be populated),
  `ranges={"field": [min, max]}`, or `magnitude_field="height"` with a
  `magnitude="building_height"` band (also `building_area`, `population_density`,
  `slope`, `elevation`, …). It then warns on missing/empty required fields,
  out-of-range or implausible values, and on any column that is entirely a
  placeholder/sentinel (`-9999`, `NULL`) — the classic failed-join signature.
- When topological correctness matters — a polygon **coverage** (admin units,
  parcels, landuse) or a line **network**, or after a dissolve/overlay — call
  `check_topology`. It flags self-intersections, duplicate geometries, overlapping
  feature pairs and coverage gaps. It's heavier than `sanity_check_result`, so use
  it deliberately, not on every output. For a **line network**, add `network=True`
  (and a `dangle_length` for the road/segment scale) to detect dangles — free line
  ends that connect to nothing.
- When a result can be confirmed by an **independent** route, cross-check it with
  `cross_check`: `mode="reasonableness"` (a number vs a known figure),
  `mode="aggregate"` (sum of per-unit values vs a known parent total — get the parent
  via `region_hierarchy`), or `mode="two_method"` (two layers/methods for the same
  quantity, e.g. LoD2 measured height vs DSM−DTM). A disagreement beyond tolerance
  means one route is wrong — investigate.\
"""


@dataclass
class GeoValidationCapability(AbstractCapability[Any]):
    """CRS and plausibility checks for vector and raster outputs."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace

        def check_crs(path: str) -> dict:
            """Report a layer's CRS and whether it is safe for measurements.

            Works for vector and raster files. Flags geographic CRS (degrees),
            which give wrong areas/distances — reproject to a metric CRS first.
            """
            path = resolve_path(path, ws)
            try:
                f = raster_facts(path) if is_raster(path) else vector_facts(path)
            except Exception as exc:  # noqa: BLE001 - surface any read/CRS error
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            authority = f["crs"]
            is_geographic = f["is_geographic"]
            if authority is None:
                return {
                    "ok": False,
                    "crs": None,
                    "warning": "layer has no CRS defined; measurements are unreliable",
                }
            return {
                "ok": not is_geographic,
                "crs": authority,
                "is_geographic": is_geographic,
                "safe_for_measurement": not is_geographic,
                "note": (
                    "geographic CRS (degrees) — reproject before measuring area/distance"
                    if is_geographic
                    else "projected CRS — area/distance are in metric units"
                ),
            }

        def sanity_check_result(
            path: str,
            min_features: int | None = 1,
            max_features: int | None = None,
            expected_geometry: str | None = None,
            required: list[str] | None = None,
            ranges: dict | None = None,
            magnitude_field: str | None = None,
            magnitude: str | None = None,
        ) -> dict:
            """Check that a vector output is plausible.

            Reports feature count, geometry types, invalid/empty geometries, CRS
            and bounds, and raises warnings when the result looks wrong (empty,
            out of the expected count range, or wrong geometry type). For rasters
            it reports size/bands/CRS instead.

            Optional attribute expectations (V1): ``required`` fields that must be
            populated, ``ranges={field: [min, max]}`` numeric bounds, and a
            plausibility band (``magnitude_field`` + ``magnitude``, e.g.
            ``"building_height"``). Warns on missing/empty required fields,
            out-of-range or implausible values, and columns that are entirely a
            placeholder/sentinel value (a failed join).
            """
            path = resolve_path(path, ws)
            try:
                if is_raster(path):
                    f = raster_facts(path)
                    return {
                        "ok": True,
                        "kind": "raster",
                        "size": [f["width"], f["height"]],
                        "bands": f["bands"],
                        "crs": f["crs"],
                        "bounds": f["bounds"],
                        "nodata": f["nodata"],
                    }
                f = vector_facts(path, full=True)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            warnings: list[str] = []
            n = f["feature_count"]
            if min_features is not None and n < min_features:
                warnings.append(f"only {n} feature(s); expected at least {min_features}")
            if max_features is not None and n > max_features:
                warnings.append(f"{n} features; expected at most {max_features}")

            geom_types = f["geometry_types"]
            if expected_geometry and not any(
                expected_geometry.lower() in gt.lower() for gt in geom_types
            ):
                warnings.append(
                    f"geometry types {geom_types} do not match expected '{expected_geometry}'"
                )

            if f["geom_null"]:
                warnings.append(f"{f['geom_null']} null geometries")
            if f["geom_empty"]:
                warnings.append(f"{f['geom_empty']} empty geometries")
            if f["geom_invalid"]:
                warnings.append(f"{f['geom_invalid']} invalid geometries")

            # Attribute-level checks (V1) — best-effort: a read failure here must
            # not sink the geometry facts already gathered.
            try:
                af = attribute_facts(path, required=required or (), ranges=ranges or None)
                for col, fc in af["fields"].items():
                    if fc["all_placeholder"]:
                        warnings.append(
                            f"column '{col}' is entirely placeholder/sentinel values "
                            "(failed join or computation?)"
                        )
                    elif ranges and col in ranges and fc["out_of_range"]:
                        warnings.append(
                            f"{fc['out_of_range']} value(s) in '{col}' outside range {ranges[col]}"
                        )
                for col in af["missing_required"]:
                    warnings.append(f"required field '{col}' is missing or empty")
            except Exception:  # noqa: BLE001 - attribute facts are advisory
                pass

            # Plausibility band on a named magnitude field (e.g. building_height).
            if magnitude_field and magnitude:
                try:
                    from pyogrio import read_dataframe

                    col = read_dataframe(
                        path, columns=[magnitude_field], read_geometry=False
                    )[magnitude_field]
                    summ = plausibility.check_series(magnitude, col.tolist())
                    if summ and summ["out_of_band"]:
                        warnings.append(
                            f"{summ['out_of_band']} value(s) in '{magnitude_field}' outside the "
                            f"plausible {magnitude} range "
                    f"[{summ['min']}, {summ['max']}] {summ['unit']}"
                        )
                except Exception:  # noqa: BLE001 - plausibility is advisory
                    pass

            return {
                "ok": not warnings,
                "kind": "vector",
                "features": n,
                "geometry_types": geom_types,
                "crs": f["crs"],
                "bounds": f["bounds"],
                "warnings": warnings,
            }

        def check_topology(
            path: str,
            check_overlaps: bool = True,
            max_features: int = 20_000,
            network: bool = False,
            dangle_length: float | None = None,
        ) -> dict:
            """Check a vector layer for topological problems (the deeper, opt-in pass).

            Heavier than ``sanity_check_result`` (which stays cheap), so call it when
            topological correctness matters — a polygon **coverage** (admin units,
            parcels, landuse) or a line **network** (roads), or after a dissolve/clip.
            Reports, all in-process (no ``qgis_process``): ``invalid`` &
            ``not_simple`` (self-intersecting) geometries, ``duplicate_geometries``,
            ``self_overlaps`` (overlapping feature pairs — overlaps in a clean
            coverage are an error) and ``union_holes`` (holes in the merged extent,
            i.e. possible gaps). The pairwise overlap/gap scan is the expensive part:
            it is skipped above ``max_features`` or with ``check_overlaps=False``
            (``overlap_checked`` reports whether it ran).

            ``network=True`` adds **dangle** detection for a line network: free line
            ends (degree-1 nodes) that connect to nothing. Every free end is counted
            (a network has legitimate dead-ends too); pass ``dangle_length`` to also
            count only *short* free-ended lines (likely digitising overshoots — the
            GRASS ``rmdangle`` idea, done in-process since this build has no runnable
            GRASS backend).
            """
            path = resolve_path(path, ws)
            if is_raster(path):
                return {"ok": False, "error": "topology checks apply to vector layers, not rasters"}
            try:
                t = topology_facts(
                    path, check_overlaps=check_overlaps, max_overlap_features=max_features
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            warnings: list[str] = []
            if t["invalid"]:
                warnings.append(f"{t['invalid']} invalid geometries "
                                "(self-intersections/ring errors)")
            if t["not_simple"]:
                warnings.append(f"{t['not_simple']} non-simple geometries (self-crossing lines)")
            if t["duplicate_geometries"]:
                warnings.append(f"{t['duplicate_geometries']} duplicate geometries")
            if t["self_overlaps"]:
                warnings.append(f"{t['self_overlaps']} overlapping feature pair(s)")
            if t["union_holes"]:
                warnings.append(
                    f"{t['union_holes']} hole(s) in the merged coverage — possible gaps "
                    "(only meaningful if this layer should be a seamless coverage)"
                )
            if check_overlaps and not t["overlap_checked"]:
                warnings.append(
                    f"overlap/gap scan skipped ({t['feature_count']} > {max_features} features); "
                    "raise max_features to include it"
                )

            out = {"kind": "vector", **t, "warnings": warnings}

            # ok = no hard topological defect. union_holes and a skipped scan are
            # informational (gaps may be legitimate; a big layer just wasn't scanned).
            defect = (t["invalid"] or t["not_simple"]
                      or t["duplicate_geometries"] or (t["self_overlaps"] or 0))

            if network:
                try:
                    d = dangle_facts(path, max_dangle_length=dangle_length)
                except Exception as exc:  # noqa: BLE001
                    d = None
                    warnings.append(f"dangle detection failed: {type(exc).__name__}")
                if d is None:
                    warnings.append("no line geometry — dangle detection applies to line networks")
                else:
                    out.update({k: d[k] for k in
                                ("free_ends", "free_end_lines", "short_dangles",
                     "max_dangle_length")})
                    if dangle_length is not None and d["short_dangles"]:
                        warnings.append(
                            f"{d['short_dangles']} short dangle(s) ≤ {dangle_length} "
                            "(likely digitising overshoots)"
                        )
                        defect = defect or d["short_dangles"]
                    elif d["free_ends"]:
                        warnings.append(
                            f"{d['free_ends']} free line end(s) — dangles if unintended "
                            "(pass dangle_length to isolate short overshoots)"
                        )

            return {"ok": not defect, **out, "warnings": warnings}

        def cross_check(  # noqa: PLR0913  # drei Pruefarten (Aggregat / Plausibilitaet /
            # Zwei-Methoden) mit je eigenen Parametern in einem Werkzeug - getrennte
            # Werkzeuge waeren fuer das Modell schwerer zu waehlen

            mode: str,
            value: float | None = None,
            expected: float | None = None,
            tolerance: float = 0.05,
            path: str | None = None,
            field: str | None = None,
            expected_total: float | None = None,
            path_b: str | None = None,
            key: str | None = None,
            field_b: str | None = None,
        ) -> dict:
            """Cross-check a result against a second number/method (validation level 3).

            Redundancy catches errors a single computation can't — the answer is
            confirmed (or contradicted) by an independent route. Three modes:

            - ``mode="reasonableness"``: is ``value`` within ``tolerance`` (relative)
              of ``expected``? The generic "is this number sane vs a known figure".
            - ``mode="aggregate"``: sum ``field`` over ``path`` (a numeric field, or
              ``"$area"``/``"$length"``) and compare to ``expected_total`` within
              ``tolerance`` — e.g. the sum of per-Gemeinde values vs the known Kreis
              total (get the parent code via ``region_hierarchy``). *Escalate the
              scope, keep the granularity* — never pass the parent aggregate off as a
              unit value.
            - ``mode="two_method"``: join ``path``.``field`` and ``path_b``.``field_b``
              on ``key`` and report the difference distribution — e.g. LoD2
              ``measured_height`` vs a DSM−DTM height, or a table vs a second source.

            ``tolerance`` is relative (0.05 = 5 %). Returns ``ok`` (within tolerance /
            good agreement) plus the numbers behind the verdict.
            """
            if mode == "reasonableness":
                if value is None or expected is None:
                    return {"ok": False, "error": "reasonableness needs value and expected"}
                dev = abs(value - expected)
                rel = dev / abs(expected) if expected else float("inf")
                return {"ok": rel <= tolerance, "mode": mode, "value": value,
                        "expected": expected, "deviation": dev, "relative": rel,
                        "tolerance": tolerance}

            if mode == "aggregate":
                if path is None or field is None or expected_total is None:
                    return {"ok": False,
                            "error": "aggregate needs path, field and expected_total"}
                p = resolve_path(path, ws)
                formula = field if field in ("$area", "$length", "$perimeter") else None
                try:
                    summ = measure_layer(p, field=None if formula else field, formula=formula)
                except KeyError:
                    return {"ok": False, "error": f"field '{field}' not found in {path}"}
                if summ is None:
                    return {"ok": False, "error": f"cannot measure '{field}'"}
                total = summ["sum"]
                dev = abs(total - expected_total)
                rel = dev / abs(expected_total) if expected_total else float("inf")
                return {"ok": rel <= tolerance, "mode": mode, "sum": total,
                        "expected_total": expected_total, "deviation": dev,
                        "relative": rel, "tolerance": tolerance, "n": summ["count"]}

            if mode == "two_method":
                if not (path and field and path_b and field_b and key):
                    return {"ok": False,
                            "error": "two_method needs path, field, path_b, field_b, key"}
                try:
                    r = compare_layers(resolve_path(path, ws), field,
                                       resolve_path(path_b, ws), field_b, key)
                except Exception as exc:  # noqa: BLE001 - missing key/column or read error
                    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                mrd = r["mean_rel_diff"]
                agree = mrd is not None and mrd <= tolerance
                out = {"ok": agree, "mode": mode, "tolerance": tolerance, **r}
                if r["compared"] == 0:
                    out["ok"] = False
                    out["warning"] = "no rows matched on the key — check the join key"
                return out

            return {"ok": False,
                    "error": f"unknown mode '{mode}'; one of reasonableness/aggregate/two_method"}

        return FunctionToolset(
            tools=[check_crs, sanity_check_result, check_topology, cross_check]
        )
