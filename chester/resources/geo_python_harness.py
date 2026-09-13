"""Run a GeoPandas snippet in a subprocess and write a JSON verdict.

    python geo_python_harness.py <code.py> <verdict.json>

The sibling of :mod:`chester.resources.qgis_python_harness`: same contract, other
namespace. That one shells into QGIS's bundled interpreter because PyQGIS must never
enter Chester's venv; this one runs in **Chester's own** interpreter, where geopandas
already lives. The subprocess is kept anyway — for the timeout, for crash isolation (a
GDAL segfault kills the child, not the agent) and to leave Chester's process state
untouched.

The verdict carries ``calls`` next to ``result``/``stdout``/``outputs``: every checked
helper the snippet used, with the warning it produced. That field is the whole reason
this is a separate harness and not a bare ``exec`` — see its comment below.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import traceback

_code_path, _out_path = sys.argv[1], sys.argv[2]

# Same spellings the model produces for a cached file; the snippet already runs *in*
# the GeoCache, so every variant has to collapse onto the CWD. Kept in step with
# `qgis_python_harness.resolve_path` — two harnesses, one path contract.
_WORKSPACE_ALIASES = (
    ".chester/workspace/",
    "chester/workspace/",
    ".selmakit/workspace/",
    "selmakit/workspace/",
    "workspace/",
)


def resolve_path(path):
    """Collapse the model's path variants to a file in the GeoCache (the CWD)."""
    p = os.path.expanduser(str(path))
    if os.path.isabs(p):
        return p
    while p.startswith("./"):
        p = p[2:]
    for alias in _WORKSPACE_ALIASES:
        if p.startswith(alias):
            p = p[len(alias) :]
            break
    if p.startswith("geocache/"):
        p = p[len("geocache/") :]
    return os.path.abspath(p)


verdict: dict = {"ok": False, "result": None, "stdout": "", "error": None,
                 "outputs": [], "calls": []}
buf = io.StringIO()


def _record(name, args, warning=None):
    """Note a checked helper's call **in the verdict's content**, not beside it.

    The point of this field, and the reason it is content and not metadata: a call
    that happens inside this subprocess is invisible to `selmakit.tool_returns`,
    which walks `ToolReturnPart`s and drops `part.metadata`. `pydantic-ai-harness`'s
    CodeMode puts its nested calls exactly there — and any validator reading tool
    returns then goes blind without a single check turning red (found 2026-09-06,
    reported upstream). Chester's gate reads the *content* of a tool return, so
    putting the calls here keeps it working with no change anywhere else.
    """
    entry = {"name": name, "args": args}
    if warning:
        entry["warning"] = warning
    verdict["calls"].append(entry)


def read_vector(path, layer=None):
    """Load a vector layer through the path contract, and say what is wrong with it.

    Reading through this instead of `gpd.read_file` buys two things the snippet
    cannot get on its own: the path spellings collapse, and a mixed-geometry layer
    announces itself *before* it is fed to something that keeps one type.
    """
    import geopandas as gpd

    resolved = resolve_path(path)
    gdf = gpd.read_file(resolved, layer=layer) if layer else gpd.read_file(resolved)
    warning = None
    with contextlib.suppress(Exception):  # a diagnostic must never break the snippet
        from chester.geofacts import mixed_geometry_note

        warning = mixed_geometry_note(sorted({g.geom_type for g in gdf.geometry if g is not None}))
    _record("read_vector", {"path": str(path), "features": len(gdf)}, warning)
    if warning:
        print(f"[read_vector] {warning}")
    return gdf


def write_vector(gdf, path, layer=None):
    """Write a layer to the GeoCache and return its absolute path.

    Also the answer to "which files did this snippet produce?". Guessing that from
    the returned `result` (as the PyQGIS side does) misses everything the snippet
    wrote but did not return; going through here makes it exact.
    """
    resolved = resolve_path(path)
    gdf.to_file(resolved, layer=layer) if layer else gdf.to_file(resolved)
    if resolved not in verdict["outputs"]:
        verdict["outputs"].append(resolved)
    _record("write_vector", {"path": str(path), "features": len(gdf)})
    return resolved


try:
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    import pyproj
    import shapely
    from shapely.geometry import (
        LineString,
        MultiLineString,
        MultiPoint,
        MultiPolygon,
        Point,
        Polygon,
        box,
        shape,
    )

    namespace = {
        "__name__": "__chester_geo_python__",
        "gpd": gpd, "geopandas": gpd, "pd": pd, "pandas": pd, "np": np, "numpy": np,
        "shapely": shapely, "pyproj": pyproj,
        "Point": Point, "LineString": LineString, "Polygon": Polygon,
        "MultiPoint": MultiPoint, "MultiLineString": MultiLineString,
        "MultiPolygon": MultiPolygon, "box": box, "shape": shape,
        "resolve_path": resolve_path,
        "read_vector": read_vector,
        "write_vector": write_vector,
    }
    # Die neun geprüften Operationen liegen **neben** dem rohen Stack im selben
    # Namensraum. Das ist der Unterschied zu CodeMode, wo nur Vorgesehenes geht: Das
    # Modell ruft `clip(...)`, wenn es passt, und schreibt `gdf[...]` von Hand, wenn
    # nicht — im selben Schnipsel. Jeder Aufruf trägt sich in `calls` ein, samt der
    # Warnung, die die Operation selbst gefunden hat.
    with contextlib.suppress(ImportError):
        from chester.geoops import OPERATIONS as _VECTOR_OPS
        from chester.networkops import OPERATIONS as _NETWORK_OPS
        from chester.rasterops import OPERATIONS as _RASTER_OPS
        from chester.terrainops import OPERATIONS as _TERRAIN_OPS

        OPERATIONS = {**_VECTOR_OPS, **_RASTER_OPS, **_TERRAIN_OPS, **_NETWORK_OPS}

        # Der Workspace muss **explizit** mit: `chester.workspace.resolve_path`
        # rechnet vom Repo-Wurzelverzeichnis aus, dieser Prozess läuft aber im
        # GeoCache. Ohne diese Zeile schreibt `reproject(…, "x.gpkg")` nach
        # `<geocache>/.chester/workspace/geocache/x.gpkg` — gemessen, derselbe
        # doppelte Pfad, der dieses Projekt schon zweimal erwischt hat.
        _WORKSPACE_ROOT = os.path.dirname(os.getcwd())

        def _checked(op_name, fn):
            def wrapper(*args, **kwargs):
                kwargs.setdefault("workspace", _WORKSPACE_ROOT)
                out = fn(*args, **kwargs)
                if isinstance(out, dict):
                    _record(op_name, {"args": [str(a)[:80] for a in args], **{
                        k: str(v)[:80] for k, v in kwargs.items()}}, out.get("warning"))
                    produced = out.get("output")
                    if produced and produced not in verdict["outputs"]:
                        verdict["outputs"].append(produced)
                    if out.get("warning"):
                        print(f"[{op_name}] {out['warning']}")
                    if not out.get("ok"):
                        raise RuntimeError(f"{op_name}: {out.get('error')}")
                return out
            wrapper.__name__ = op_name
            wrapper.__doc__ = fn.__doc__
            return wrapper

        for _name, _fn in OPERATIONS.items():
            namespace[_name] = _checked(_name, _fn)
    with contextlib.suppress(ImportError):  # optional: not every task touches raster
        import rasterio

        namespace["rasterio"] = rasterio

    with open(_code_path, "r", encoding="utf-8") as fh:
        user_code = fh.read()
    with contextlib.redirect_stdout(buf):
        exec(compile(user_code, "<geo_python>", "exec"), namespace)  # noqa: S102

    verdict["ok"] = True
    value = namespace.get("result")
    try:
        json.dumps(value)  # keep only JSON-serialisable results verbatim
        verdict["result"] = value
    except (TypeError, ValueError):
        verdict["result"] = repr(value)
except Exception:  # noqa: BLE001 - any failure is reported as the verdict
    verdict["error"] = traceback.format_exc()
finally:
    verdict["stdout"] = buf.getvalue()

with open(_out_path, "w", encoding="utf-8") as fh:
    json.dump(verdict, fh, default=str)
