"""Run arbitrary PyQGIS code headless in QGIS's own bundled Python.

The companion to :mod:`chester.qgis_process`. Where that runs a single QGIS
*algorithm* via the ``qgis_process`` CLI, this runs an arbitrary PyQGIS
*snippet* — for multi-step computation, per-feature iteration, or geometry math
the algorithm tools can't express. Same boundary as ``qgis_process``: it shells
out to QGIS's bundled interpreter (:func:`chester.qgis_env.resolve_qgis_python_env`)
and never imports PyQGIS into Chester's venv.

The snippet runs under :mod:`chester.resources.qgis_python_harness`, which
initialises QGIS + Processing, execs the code, and writes a JSON verdict
``{ok, result, stdout, error}`` to a file (stdout is full of Qt noise, so the
verdict travels out-of-band).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from chester.qgis_env import resolve_qgis_python_env

DEFAULT_TIMEOUT = 300  # seconds; a snippet may run several algorithms

_HARNESS = Path(__file__).resolve().parent / "resources" / "qgis_python_harness.py"


class QgisPythonError(RuntimeError):
    """Raised when the PyQGIS subprocess can't run or produces no verdict."""


def run_pyqgis(
    code: str,
    *,
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Execute ``code`` as PyQGIS and return the harness verdict.

    ``cwd`` is the working directory for the subprocess (Chester passes the
    GeoCache dir, so a bare output filename in the snippet lands in the cache).
    Returns ``{"ok": bool, "result": ..., "stdout": str, "error": str|None}``.

    Raises:
        QgisPythonError: on timeout, or if the harness wrote no verdict (e.g. the
            interpreter crashed before finishing).
    """
    penv = resolve_qgis_python_env()  # raises QgisNotFoundError
    with tempfile.TemporaryDirectory(prefix="chester-pyqgis-") as td:
        code_path = Path(td) / "user_code.py"
        out_path = Path(td) / "verdict.json"
        code_path.write_text(code, encoding="utf-8")
        cmd = [
            str(penv.bin),
            str(_HARNESS),
            penv.prefix,
            penv.plugins or "",
            penv.providers or "",
            penv.pkgdata or "",
            str(code_path),
            str(out_path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                env=penv.subprocess_env(),
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise QgisPythonError(
                f"PyQGIS code timed out after {timeout}s"
            ) from exc

        if not out_path.exists():
            detail = (proc.stderr or proc.stdout or "(no output)").strip()
            raise QgisPythonError(
                f"PyQGIS harness produced no verdict (exit {proc.returncode}): "
                f"{detail[-800:]}"
            )
        return json.loads(out_path.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    out = run_pyqgis(
        "from qgis.core import QgsApplication\n"
        "result = len(QgsApplication.processingRegistry().algorithms())\n"
        "print('hello from pyqgis')\n"
    )
    print(out)
