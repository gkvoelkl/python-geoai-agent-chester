"""Central QGIS environment discovery for headless ``qgis_process`` calls.

QGIS ships its own bundled Python and data files (PROJ, GDAL). Rather than
importing PyQGIS into Chester's venv, every GIS operation shells out to the
``qgis_process`` CLI. This module locates that binary and builds the environment
it needs to run headless and to find ``proj.db`` (without which reprojection is
silently wrong).

Override discovery with environment variables:
    CHESTER_QGIS_PROCESS_BIN   full path to the qgis_process binary
    CHESTER_QGIS_APP           path to the QGIS .app bundle / install prefix
    CHESTER_QGIS_PYTHON_BIN    full path to QGIS's bundled Python interpreter
                               (used by :func:`resolve_qgis_python_env`)
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path


class QgisNotFoundError(RuntimeError):
    """Raised when no usable ``qgis_process`` binary can be located."""


def _candidate_binaries() -> list[Path]:
    """Return possible ``qgis_process`` locations, most explicit first."""
    candidates: list[Path] = []

    env_bin = os.environ.get("CHESTER_QGIS_PROCESS_BIN")
    if env_bin:
        candidates.append(Path(env_bin))

    env_app = os.environ.get("CHESTER_QGIS_APP")
    if env_app:
        candidates.append(Path(env_app) / "Contents" / "MacOS" / "qgis_process")
        candidates.append(Path(env_app) / "bin" / "qgis_process")

    # macOS application bundles, e.g. /Applications/QGIS-final-4_0_3.app
    for app in sorted(glob.glob("/Applications/QGIS*.app"), reverse=True):
        candidates.append(Path(app) / "Contents" / "MacOS" / "qgis_process")

    # Linux / PATH install
    candidates.append(Path("/usr/bin/qgis_process"))
    candidates.append(Path("/usr/local/bin/qgis_process"))

    return candidates


@dataclass(frozen=True)
class QgisEnv:
    """A resolved, ready-to-use QGIS execution environment."""

    bin: Path
    env: dict[str, str]

    def subprocess_env(self) -> dict[str, str]:
        """Return ``os.environ`` merged with QGIS-specific overrides."""
        merged = dict(os.environ)
        merged.update(self.env)
        return merged


def resolve_qgis_env() -> QgisEnv:
    """Locate ``qgis_process`` and assemble its headless environment.

    Raises:
        QgisNotFoundError: if no binary is found at any known location.
    """
    binary = next((c for c in _candidate_binaries() if c.exists()), None)
    if binary is None:
        raise QgisNotFoundError(
            "qgis_process not found. Install QGIS or set CHESTER_QGIS_PROCESS_BIN "
            "/ CHESTER_QGIS_APP."
        )

    env: dict[str, str] = {"QT_QPA_PLATFORM": "offscreen"}

    # Derive the bundled PROJ and GDAL data dirs from the binary location.
    # macOS bundle layout: <App>.app/Contents/MacOS/qgis_process
    #                      <App>.app/Contents/Resources/qgis/{proj,gdal}
    contents = binary.parent.parent  # .../Contents (mac) or install prefix
    for resources in (contents / "Resources" / "qgis", contents / "share" / "qgis"):
        proj_dir = resources / "proj"
        gdal_dir = resources / "gdal"
        if (proj_dir / "proj.db").exists():
            env["PROJ_DATA"] = str(proj_dir)
            env["PROJ_LIB"] = str(proj_dir)  # older PROJ honours PROJ_LIB
        if gdal_dir.exists():
            env["GDAL_DATA"] = str(gdal_dir)

    return QgisEnv(bin=binary, env=env)


@dataclass(frozen=True)
class QgisPythonEnv:
    """A resolved environment for running arbitrary PyQGIS in QGIS's own Python.

    Where :class:`QgisEnv` runs the ``qgis_process`` CLI (one algorithm at a
    time), this runs QGIS's *bundled Python interpreter* on a standalone script,
    so arbitrary PyQGIS code executes headless and exits cleanly. Nothing is
    imported into Chester's venv — the same boundary as ``qgis_process``.
    """

    bin: Path  # QGIS's bundled python3 interpreter
    prefix: str  # QGIS prefix path for QgsApplication.setPrefixPath
    plugins: str | None  # the built-in plugins dir (holds `processing`)
    env: dict[str, str]

    def subprocess_env(self) -> dict[str, str]:
        merged = dict(os.environ)
        merged.update(self.env)
        return merged


def resolve_qgis_python_env() -> QgisPythonEnv:
    """Locate QGIS's bundled Python and assemble a standalone-PyQGIS environment.

    Builds on :func:`resolve_qgis_env` (inherits the offscreen + PROJ/GDAL env),
    then adds the pieces a standalone interpreter needs: ``PYTHONHOME`` (so the
    bundled interpreter finds its own stdlib), ``PYTHONPATH`` (the ``qgis``
    package's site-packages), ``QGIS_PREFIX_PATH``, and the built-in plugins dir
    (for ``import processing``). Verified against the macOS ``.app`` layout;
    a prefix/Linux install is handled best-effort via the same probes.

    Raises:
        QgisNotFoundError: if the interpreter or the ``qgis`` package is missing.
    """
    base = resolve_qgis_env()  # raises QgisNotFoundError
    env = dict(base.env)  # QT_QPA_PLATFORM=offscreen + PROJ_DATA/GDAL_DATA
    contents = base.bin.parent.parent  # .../Contents (mac) or install prefix

    # ── the interpreter ──────────────────────────────────────────────────
    py_bin: Path | None = None
    override = os.environ.get("CHESTER_QGIS_PYTHON_BIN")
    if override:
        py_bin = Path(override)
    else:
        for cand in sorted((contents / "MacOS").glob("python3.*"), reverse=True):
            if cand.is_file() and os.access(cand, os.X_OK):
                py_bin = cand
                break
        if py_bin is None:
            for cand in (contents / "bin" / "python3", Path("/usr/bin/python3")):
                if cand.exists():
                    py_bin = cand
                    break
    if py_bin is None or not py_bin.exists():
        raise QgisNotFoundError(
            "QGIS bundled Python not found. Set CHESTER_QGIS_PYTHON_BIN to the "
            "interpreter shipped with QGIS."
        )

    # ── the `qgis` package (site-packages) ───────────────────────────────
    site: Path | None = None
    for root in (contents / "Resources", contents / "share" / "qgis" / "python", contents):
        for cand in sorted(root.glob("python3.*/site-packages"), reverse=True):
            if (cand / "qgis").is_dir():
                site = cand
                break
        if site:
            break
    if site is None:  # last resort: search a bit wider before giving up
        for cand in contents.glob("**/site-packages"):
            if (cand / "qgis").is_dir():
                site = cand
                break
    if site is None:
        raise QgisNotFoundError(
            f"Could not locate the 'qgis' Python package under {contents}."
        )
    env["PYTHONPATH"] = str(site)

    # ── the built-in plugins dir (holds `processing`) ────────────────────
    plugins: str | None = None
    for cand in (
        contents / "Resources" / "qgis" / "python" / "plugins",
        contents / "share" / "qgis" / "python" / "plugins",
    ):
        if cand.is_dir():
            plugins = str(cand)
            break

    # ── PYTHONHOME (bundled stdlib) ──────────────────────────────────────
    for home in (contents / "Frameworks", contents):
        if any(home.glob("lib/python3.*")):
            env["PYTHONHOME"] = str(home)
            break

    prefix = contents / "MacOS"
    prefix_str = str(prefix) if prefix.is_dir() else str(contents)
    env["QGIS_PREFIX_PATH"] = prefix_str

    return QgisPythonEnv(bin=py_bin, prefix=prefix_str, plugins=plugins, env=env)


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import subprocess

    qgis = resolve_qgis_env()
    print(f"qgis_process: {qgis.bin}")
    print(f"env overrides: {qgis.env}")
    out = subprocess.run(
        [str(qgis.bin), "--version"],
        env=qgis.subprocess_env(),
        capture_output=True,
        text=True,
    )
    print(out.stdout.strip() or out.stderr.strip())

    py = resolve_qgis_python_env()
    print(f"\nqgis python: {py.bin}")
    print(f"prefix: {py.prefix}")
    print(f"plugins: {py.plugins}")
    print(f"PYTHONHOME: {py.env.get('PYTHONHOME')}")
    print(f"PYTHONPATH: {py.env.get('PYTHONPATH')}")
