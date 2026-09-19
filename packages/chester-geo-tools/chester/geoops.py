"""The eleven vector operations, on GeoPandas — pure, no agent stack.

Phase KQ step 2 (`internal/TODO.md`): the same operations `qgis_clip` &co. run
through `qgis_process`, written against geopandas so they need no QGIS installation.
Pure like `geofacts`/`geocache`: no SelmaKit, no `chester.capabilities`, so both sides
can import it — the tools that expose them and the snippet namespace of
`geo_python_run`, where they sit next to raw `gpd`.

Every function takes and returns **paths**, resolves them through the one path
contract, and answers with facts rather than a bare success:
``{ok, output, features_in, features_out, crs, warning?}``.

Two traps are encoded here because they cost this project runs, and neither is
geopandas-specific:

* **Metric work in a geographic CRS.** A 500 in degrees is not 500 m. `buffer` refuses
  it outright instead of returning a plausible-looking wrong shape.
* **Voll rein, leer raus.** An empty result from a non-empty input means the step did
  nothing; saying so is the whole lesson of 2026-09-05.

What geopandas gives for free, and QGIS does not: a mixed-geometry layer survives an
overlay. `native:clip` writes ONE geometry type — the one the file's header declares —
and drops the rest silently; measured 2026-09-05, that turned 246 supermarkets into 18.
"""

from __future__ import annotations

import difflib
import functools
import os
from typing import Any

from chester import provenance
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

#: Operations whose result is only meaningful in metres. A geographic CRS (degrees)
#: makes their numbers wrong in a way that looks right — the oldest trap in the bank.
_METRIC_OPS = ("buffer", "field_sum:area", "field_sum:length")


class LayerNotFound(FileNotFoundError):
    """Eine Eingabeebene, die es nicht gibt — mit dem Pfad, wie er gemeint war."""


def _read(path: str, ws: str):
    import geopandas as gpd

    resolved = resolve_path(path, ws)
    if not os.path.isfile(resolved):
        raise LayerNotFound(path)
    return gpd.read_file(resolved)


def _near(path: str, ws: str) -> list[str]:
    """Vorhandene Ebenen, deren Name dem gesuchten ähnelt."""
    folder = os.path.dirname(resolve_path("x.gpkg", ws))
    try:
        have = [f for f in os.listdir(folder) if not f.endswith(".meta.json")]
    except OSError:
        return []
    name = os.path.basename(path)
    close = difflib.get_close_matches(name, have, n=3, cutoff=0.5)
    return close or sorted(have)[:8]


def _reports_missing_layers(fn):
    """Eine fehlende Eingabe ist eine Absage, kein Absturz.

    Gemessen 2026-09-07 (`supermarket-accessibility-choropleth`): Das Modell schrieb
    `regress_supermarkets_split_polygon.gpkg` statt `regensburg_…` — ein Wort daneben.
    `gpd.read_file` warf `DataSourceError`, die Ausnahme verliess das Werkzeug und
    **beendete den ganzen Lauf** nach 930 s. Ein Tippfehler darf höchstens einen
    Aufruf kosten, und der Rückgabekanal kann sagen, was wirklich da liegt — das ist
    dieselbe Stelle, an der schon `_did_nothing_warning` und der Guard ansetzen.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ws = kwargs.get("workspace", DEFAULT_WORKSPACE)
        try:
            return fn(*args, **kwargs)
        except LayerNotFound as exc:
            wanted = str(exc)
            return {"ok": False,
                    "error": f"no layer {wanted!r} in the workspace — nothing was "
                             "read and nothing written. Check the spelling against "
                             "the paths earlier tools returned.",
                    "did_you_mean": _near(wanted, ws)}
    return wrapper


def _facts(gdf_in, gdf_out, output: str) -> dict:
    """The common answer: what went in, what came out, and whether that is nothing."""
    out: dict[str, Any] = {
        "ok": True,
        "output": output,
        "features_in": len(gdf_in),
        "features_out": len(gdf_out),
        "crs": str(gdf_out.crs) if gdf_out.crs else None,
    }
    if len(gdf_in) and not len(gdf_out):
        out["warning"] = (
            f"the output is EMPTY: {len(gdf_in)} feature(s) went in, 0 came out. The "
            "file exists, but the step did nothing — do not build on it. Usually the "
            "layers do not overlap (compare the CRS and the bounds) or the filter "
            "matched nothing."
        )
    return out


def _write(gdf, output_path: str, ws: str, tool: str, query: str | None = None) -> str:
    resolved = resolve_path(output_path, ws, write=True)
    gdf.to_file(resolved)
    provenance.write_meta(resolved, source="chester", tool=tool, query=query)
    return resolved


def _align(gdf, other, ws: str):
    """Bring ``other`` onto ``gdf``'s CRS — an overlay across CRSs is meaningless."""
    if gdf.crs is not None and other.crs is not None and gdf.crs != other.crs:
        other = other.to_crs(gdf.crs)
    return other


@_reports_missing_layers
def reproject(input_path: str, output_path: str, target_crs: str,
              workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Transform a layer to ``target_crs`` (e.g. "EPSG:25832")."""
    gdf = _read(input_path, workspace)
    out = gdf.to_crs(target_crs)
    return _facts(gdf, out, _write(out, output_path, workspace, "reproject", target_crs))


@_reports_missing_layers
def buffer(input_path: str, output_path: str, distance: float,
           workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Buffer every feature by ``distance`` **in the layer's own units**.

    Refuses a geographic CRS: there, `distance` is degrees, and 500 degrees is not
    500 metres. Returning the shape anyway is how a run reports a wrong number with a
    straight face.
    """
    gdf = _read(input_path, workspace)
    if gdf.crs is not None and gdf.crs.is_geographic:
        return {
            "ok": False,
            "error": (
                f"{gdf.crs} is a geographic CRS — `distance` would be DEGREES, not "
                f"metres, and {distance}° is roughly {distance * 111:.0f} km. Reproject "
                "to a metric CRS first (EPSG:25832 for Germany, 25833 east of 12°E in "
                "Brandenburg/Saxony, 2056 for Switzerland, 31287 for Austria)."
            ),
        }
    out = gdf.copy()
    out["geometry"] = gdf.geometry.buffer(distance)
    return _facts(gdf, out, _write(out, output_path, workspace, "buffer", str(distance)))


@_reports_missing_layers
def clip(input_path: str, overlay_path: str, output_path: str,
         workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Cut ``input_path`` to the outline of ``overlay_path``.

    Unlike `native:clip` this keeps **every** geometry type the input holds — the
    single-type loss that cost 138 of 246 supermarkets on 2026-09-05 cannot happen.
    """
    import geopandas as gpd

    gdf = _read(input_path, workspace)
    mask = _align(gdf, _read(overlay_path, workspace), workspace)
    out = gpd.clip(gdf, mask, keep_geom_type=False)
    return _facts(gdf, out, _write(out, output_path, workspace, "clip", overlay_path))


@_reports_missing_layers
def intersection(input_path: str, overlay_path: str, output_path: str,
                 workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Geometric intersection, keeping the attributes of both layers."""
    import geopandas as gpd

    gdf = _read(input_path, workspace)
    other = _align(gdf, _read(overlay_path, workspace), workspace)
    out = gpd.overlay(gdf, other, how="intersection", keep_geom_type=False)
    return _facts(gdf, out, _write(out, output_path, workspace, "intersection", overlay_path))


@_reports_missing_layers
def extract_by_location(input_path: str, overlay_path: str, output_path: str,
                        predicate: str = "intersects",
                        workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Keep whole features related to ``overlay_path`` (intersects/within/contains).

    Selection, not cutting: a feature survives **whole** or not at all. Use `clip`
    when the geometry should be cut at the border — confusing the two is the
    `intersection-not-selection` probe.
    """
    gdf = _read(input_path, workspace)
    other = _align(gdf, _read(overlay_path, workspace), workspace)
    hit = gdf.geometry.apply(
        lambda g: bool(other.geometry.apply(lambda o: getattr(g, predicate)(o)).any())
    )
    out = gdf[hit]
    return _facts(gdf, out, _write(out, output_path, workspace, "extract_by_location", predicate))


@_reports_missing_layers
def extract_by_attribute(input_path: str, output_path: str, expression: str,
                         workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Keep features matching a pandas query, e.g. ``"height > 15"``."""
    gdf = _read(input_path, workspace)
    try:
        out = gdf.query(expression)
    except Exception as exc:  # noqa: BLE001 - the expression is the model's, name the fault
        return {"ok": False, "error": f"could not evaluate {expression!r}: "
                                      f"{type(exc).__name__}: {exc}",
                "available_columns": [c for c in gdf.columns if c != gdf.geometry.name][:40]}
    return _facts(gdf, out, _write(out, output_path, workspace, "extract_by_attribute", expression))


@_reports_missing_layers
def dissolve(input_path: str, output_path: str, by: str | None = None,
             workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Merge geometries, optionally grouped by a column."""
    gdf = _read(input_path, workspace)
    out = gdf.dissolve(by=by).reset_index() if by else gdf.dissolve().reset_index(drop=True)
    return _facts(gdf, out, _write(out, output_path, workspace, "dissolve", by))


@_reports_missing_layers
def merge(input_paths: list[str], output_path: str,
          workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Stack several layers into one, bringing them onto a common CRS first.

    The counterpart of `split_by_geometry`, and the operation that was missing when
    the twenty were built — measured 2026-09-07 (`buffer-schools-500m`): the agent
    split a mixed school layer, reprojected the parts, and then had to reach for a
    raw ``pd.concat`` to put them back together, because nothing checked did it. That
    one file came out without a provenance sidecar while the other ten had one.

    What a bare ``pd.concat`` does with the CRS, measured against geopandas 1.x
    rather than assumed:

    * **Two different, known CRSs** → it raises ``ValueError: Cannot determine common
      CRS``. Loud, so nothing is lost — but the model then has to work around it, and
      the workaround is where the mistakes live. Here the layers are simply
      reprojected onto the first CRS found, and which ones had to move is reported.
    * **One layer without a CRS** → it adopts the other layer's CRS and emits a
      *warning*, leaving the untransformed coordinates in place (verified: a point at
      12.1° stays 12.1 in a layer now labelled EPSG:25832). That is the dangerous
      case, because a warning raised inside a `geo_python_run` subprocess reaches
      nobody. This refuses instead: there is nothing to align an unreferenced layer
      by, and guessing that it is "probably already in the target system" is how a
      layer comes to claim one system while holding two.

    It also says when the merge has re-created a mixed-geometry layer — the state
    `split_by_geometry` exists to undo.
    """
    import geopandas as gpd
    import pandas as pd

    from chester.geofacts import mixed_geometry_note

    paths = list(input_paths or [])
    if len(paths) < 2:
        return {"ok": False, "error": f"merge needs at least two layers, got {len(paths)}"}

    frames = [_read(p, workspace) for p in paths]
    target = next((f.crs for f in frames if f.crs is not None), None)
    unknown = [p for p, f in zip(paths, frames, strict=True) if f.crs is None]
    if target is not None and unknown:
        return {"ok": False, "error": (
            f"{', '.join(unknown)} carr{'ies' if len(unknown) == 1 else 'y'} no CRS "
            f"while the other layer(s) are in {target}. Stacking them would put "
            "coordinates from two systems into one layer without any sign of it. Set "
            "the missing CRS on that file first — whatever produced it knows which "
            "one it is.")}

    reprojected = []
    for i, (path, frame) in enumerate(zip(paths, frames, strict=True)):
        if target is not None and frame.crs != target:
            frames[i] = frame.to_crs(target)
            reprojected.append(path)

    out = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=target)
    resolved = _write(out, output_path, workspace, "merge", f"{len(paths)} layers")
    facts: dict[str, Any] = {
        "ok": True,
        "output": resolved,
        "features_in": sum(len(f) for f in frames),
        "features_out": len(out),
        "crs": str(target) if target else None,
        "layers": [{"path": p, "features": len(f)}
                   for p, f in zip(paths, frames, strict=True)],
    }
    if reprojected:
        facts["reprojected_to_match"] = reprojected
    # Spalten, die nicht überall vorkamen, sind jetzt teilweise leer. Kein Fehler,
    # aber der Grund, warum ein späteres Filter auf so einer Spalte weniger findet
    # als erwartet — also sichtbar machen, statt es entdecken zu lassen.
    shared = set.intersection(*(set(f.columns) for f in frames))
    partial = sorted(set().union(*(set(f.columns) for f in frames)) - shared)
    if partial:
        facts["columns_only_in_some_layers"] = partial[:20]
    note = mixed_geometry_note(sorted(set(out.geom_type.dropna())))
    if note:
        facts["mixed_geometry"] = True
        facts["warning"] = "this merge produced a mixed-geometry layer. " + note
    return facts


@_reports_missing_layers
def join(input_path: str, table_path: str, output_path: str, *,
         field: str, table_field: str | None = None,
         workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Join a table (CSV/GeoPackage/…) onto a layer by a shared key column.

    The QGIS-free `native:joinattributestable`, and the one operation of the twenty-
    one whose *silent* failure mode is famous enough to have its own probe
    (`join-leading-zero-ags`): a join matches on value **and type**, and the integer
    9375117 is not the text ``"09375117"``. Every Bavarian AGS starts with the state
    key 09, so reading it as a number drops the zero and nothing matches. The output
    then holds every polygon, carries the joined column, and is empty in every row.

    So this refuses to be quiet about it: it reports `joined` and `unjoined`, and when
    the keys look like they differ only in type or padding it says so and does not
    pretend the join worked.
    """
    import pandas as pd

    gdf = _read(input_path, workspace)
    other = resolve_path(table_path, workspace)
    table = (pd.read_csv(other, dtype=str) if other.lower().endswith(".csv")
             else _read(table_path, workspace).drop(columns="geometry", errors="ignore"))
    key_right = table_field or field
    for label, frame, column in (("layer", gdf, field), ("table", table, key_right)):
        if column not in frame.columns:
            return {"ok": False, "error": (
                f"the {label} has no column {column!r}. Available: "
                f"{[c for c in frame.columns if c != 'geometry'][:30]}")}

    # Beide Schlüssel als Text vergleichen — der Typ ist die Falle, nicht der Wert.
    left_key, right_key = gdf[field].astype(str), table[key_right].astype(str)
    out = gdf.copy()
    out["__key"] = left_key
    joined = out.merge(table.assign(__key=right_key).drop(columns=[key_right]),
                       on="__key", how="left").drop(columns="__key")
    added = [c for c in table.columns if c != key_right]
    matched = int(joined[added[0]].notna().sum()) if added else 0

    facts = _facts(gdf, joined, _write(joined, output_path, workspace, "join", field))
    facts.update({"joined": matched, "unjoined": len(gdf) - matched,
                  "columns_added": added[:20]})
    if matched < len(gdf):
        left_w = {len(v) for v in left_key.dropna().unique()}
        right_w = {len(v) for v in right_key.dropna().unique()}
        padding = (left_w != right_w and
                   any(a != b and a.lstrip("0") == b.lstrip("0")
                       for a in list(left_key)[:200] for b in list(right_key)[:200]))
        facts["warning"] = (
            f"{len(gdf) - matched} of {len(gdf)} feature(s) found NO match, so those "
            "rows carry empty values — the file looks complete and is not."
            + (" The keys differ only in leading zeros (widths "
               f"{sorted(left_w)} vs {sorted(right_w)}): a key read as a number lost "
               "its zero. Pad the shorter side before joining — an AGS is text, not a "
               "count." if padding else
               " Compare a few keys from both sides with `vector_info(values_of=…)`: "
               "a join matches on value AND type."))
    return facts


@_reports_missing_layers
def add_field(input_path: str, output_path: str, name: str, expression: str,
              workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Add a column computed from a pandas expression over the existing ones.

    ``area`` and ``length`` are available as names; both are computed from the
    geometry and are only metres if the layer is in a metric CRS — which is checked,
    because an area in square degrees is the same trap as a buffer in degrees.
    """
    gdf = _read(input_path, workspace)
    scope = {c: gdf[c] for c in gdf.columns if c != gdf.geometry.name}
    geographic = gdf.crs is not None and gdf.crs.is_geographic
    if "area" in expression or "length" in expression:
        if geographic:
            return {
                "ok": False,
                "error": (f"{gdf.crs} is a geographic CRS — an area computed there is in "
                          "square degrees, not m². Reproject to a metric CRS first."),
            }
        scope["area"] = gdf.geometry.area
        scope["length"] = gdf.geometry.length
    out = gdf.copy()
    try:
        import pandas as pd

        out[name] = pd.eval(expression, local_dict=scope, engine="python")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not evaluate {expression!r}: "
                                      f"{type(exc).__name__}: {exc}",
                "available_columns": list(scope)[:40]}
    facts = _facts(gdf, out, _write(out, output_path, workspace, "add_field", expression))
    facts["column"] = name
    return facts


@_reports_missing_layers
def field_sum(input_path: str, column: str, workspace: str = DEFAULT_WORKSPACE) -> dict:
    """Sum a numeric column — the answer, not a file.

    ``column`` may be ``"area"`` or ``"length"``; both are then computed from the
    geometry and require a metric CRS.
    """
    gdf = _read(input_path, workspace)
    geographic = gdf.crs is not None and gdf.crs.is_geographic
    if column in ("area", "length"):
        if geographic:
            return {"ok": False,
                    "error": (f"{gdf.crs} is a geographic CRS — {column} would be in "
                              "degrees. Reproject to a metric CRS first.")}
        series = gdf.geometry.area if column == "area" else gdf.geometry.length
    elif column not in gdf.columns:
        return {"ok": False, "error": f"no column {column!r}",
                "available_columns": [c for c in gdf.columns if c != gdf.geometry.name][:40]}
    else:
        series = gdf[column]
    try:
        total = float(series.sum())
    except (TypeError, ValueError):
        return {"ok": False, "error": f"column {column!r} is not numeric "
                                      f"(dtype {series.dtype}) — a sum has no meaning"}
    return {"ok": True, "column": column, "sum": total, "features": len(gdf),
            "crs": str(gdf.crs) if gdf.crs else None}


#: The nine, by the name the snippet namespace uses. One place, so the tools and the
#: sandbox cannot drift apart.
OPERATIONS = {
    "reproject": reproject,
    "buffer": buffer,
    "clip": clip,
    "intersection": intersection,
    "extract_by_location": extract_by_location,
    "extract_by_attribute": extract_by_attribute,
    "dissolve": dissolve,
    "merge": merge,
    "join": join,
    "add_field": add_field,
    "field_sum": field_sum,
}
