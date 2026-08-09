"""Standalone-PyQGIS harness — runs INSIDE QGIS's bundled Python.

Launched by :mod:`chester.qgis_python` as::

    <qgis python> qgis_python_harness.py <prefix> <plugins> <code.py> <result.json>

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

_prefix, _plugins, _code_path, _out_path = sys.argv[1:5]

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
            p = p[len(alias):]
            break
    if p.startswith("geocache/"):
        p = p[len("geocache/"):]
    return os.path.abspath(p)  # relative to the CWD, which is the GeoCache dir

from qgis.core import QgsApplication  # noqa: E402  (needs prefix set below)

QgsApplication.setPrefixPath(_prefix, True)
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

    namespace = {
        "__name__": "__chester_qgis_python__",
        "processing": processing,
        "resolve_path": resolve_path,  # available to the snippet, see instructions
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
