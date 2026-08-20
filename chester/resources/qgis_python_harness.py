"""Standalone-PyQGIS harness — runs INSIDE QGIS's bundled Python.

Launched by :mod:`chester.qgis_python` as::

    <qgis python> qgis_python_harness.py <prefix> <plugins> <providers> \
                                         <pkgdata> <code.py> <result.json>

It initialises a headless QGIS application + the Processing framework, execs the
user's snippet in a namespace where ``processing`` (and the ``qgis.*`` modules)
are importable, captures its stdout and an optional ``result`` variable, and
writes a JSON verdict to ``<result.json>``. QGIS/Qt print noise to the real
stdout/stderr, so the verdict goes to its own file — never parsed out of stdout.

Only QGIS-bundled + stdlib imports here; this file must never pull in Chester's
venv dependencies (it runs in QGIS's interpreter, not Chester's).
"""

import contextlib
import io
import json
import os
import sys
import traceback

_prefix, _plugins, _providers, _pkgdata, _code_path, _out_path = sys.argv[1:7]

# Leading directory prefixes the model invents that really mean "the workspace".
# Mirrors chester.workspace._WORKSPACE_ALIASES, kept as a self-contained copy
# because this harness runs in QGIS's Python and must not import Chester.
_WORKSPACE_ALIASES = (
    ".chester/workspace/",
    "chester/workspace/",
    ".selmakit/workspace/",
    "selmakit/workspace/",
    "workspace/",
)


def resolve_path(path):
    """Collapse the model's path variants to a file in the GeoCache (the CWD).

    Chester's other tools return workspace-prefixed paths like
    ``.chester/workspace/geocache/x.gpkg``, but this snippet already runs *inside*
    the GeoCache dir. Passing such a prefix verbatim to ``QgsVectorLayer`` fails
    (the layer is silently invalid), and ``os.path.abspath`` on it doubles the
    path. Stripping the workspace/``geocache/`` prefix and anchoring to the CWD
    makes every spelling resolve to the same cache file. Absolute paths pass
    through unchanged.
    """
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
    return os.path.abspath(p)  # relative to the CWD, which is the GeoCache dir


# The provider registry is a singleton whose plugin directory is fixed by the
# **first** call — every later `setPluginPath` is ignored, and so is
# `QGIS_PLUGINPATH` (all three measured 2026-08-19). QGIS derives that directory
# from the macOS prefix as `<prefix>/Contents/PlugIns/qgis`, one `Contents` too
# many, so it finds nothing and keeps only the 17 providers compiled into the core
# library: no `postgres`, no `wms`, no `wfs`, no `spatialite`. A snippet opening
# such a layer then gets `isValid() == False` and an empty error string — the
# failure looks like a bad URI. Seeding the registry here, before anything else
# touches QGIS, is the one moment where the path can still be set.
from qgis.core import QgsProviderRegistry  # noqa: E402  (must precede QgsApplication)

if _providers:
    QgsProviderRegistry.instance(_providers)

from qgis.core import QgsApplication  # noqa: E402  (needs prefix set below)

QgsApplication.setPrefixPath(_prefix, True)
if _pkgdata:
    # Same wrong derivation as the providers, one level on: without this the
    # bundled SVG library is missed and every SvgMarker draws as a "?".
    QgsApplication.setPkgDataPath(_pkgdata)
_qgs = QgsApplication([], False)  # False = no GUI
_qgs.initQgis()

verdict = {"ok": False, "result": None, "stdout": "", "error": None}
buf = io.StringIO()
try:
    if _plugins:
        sys.path.append(_plugins)  # so `import processing` resolves
    from processing.core.Processing import Processing

    Processing.initialize()
    import processing  # noqa: F401  (exposed to the user snippet)

    # Every ``Qgs*`` class preloaded, the way QGIS's own Python console does it
    # (`from qgis.core import *`). Models write console-style snippets — without
    # this they either forget the import (``NameError: QgsVectorLayer``) or invent
    # one that does not exist (``ImportError: QgsProcessingFeatureSink``); two of
    # five failed snippets in one benchmark run were exactly that, each costing a
    # model turn. Only names are bound, no work: qgis.core is already imported.
    import qgis.core

    namespace = {
        "__name__": "__chester_qgis_python__",
        "processing": processing,
        "resolve_path": resolve_path,  # available to the snippet, see instructions
        **{name: getattr(qgis.core, name) for name in dir(qgis.core) if name.startswith("Qgs")},
    }
    with open(_code_path, "r", encoding="utf-8") as fh:
        user_code = fh.read()
    with contextlib.redirect_stdout(buf):
        exec(compile(user_code, "<qgis_python>", "exec"), namespace)  # noqa: S102

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
    _qgs.exitQgis()

with open(_out_path, "w", encoding="utf-8") as fh:
    json.dump(verdict, fh, default=str)
