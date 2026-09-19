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
    CHESTER_GRASS_APP          path to the GRASS .app bundle / install prefix
                               (see :func:`find_gisbase`)
    CHESTER_NO_QGIS            set to 1/true/yes to pretend QGIS is absent, whatever
                               is installed (see :func:`qgis_disabled`)
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path


class QgisNotFoundError(RuntimeError):
    """Raised when no usable ``qgis_process`` binary can be located."""


def _candidate_binaries() -> list[Path]:
    """Return possible ``qgis_process`` locations, most explicit first.

    The one chokepoint every QGIS lookup goes through, which is why the off-switch
    sits here: pointing the env vars at nowhere is not enough, because the
    `/Applications` scan below finds an installed QGIS regardless (measured
    2026-09-06). GRASS is deliberately **not** affected — it is a separate install
    and hydrology should keep working in the QGIS-less mode.
    """
    if qgis_disabled():
        return []
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


def qgis_disabled() -> bool:
    """Whether QGIS is switched **off** on purpose, however much of it is installed.

    Two ways to set it: ``geodata.use_qgis: false`` in ``.chester/chester.json`` —
    the normal choice — and ``CHESTER_NO_QGIS=1`` as an override for a single run.

    Two reasons it exists at all, and neither is a preference:

    * **The QGIS-less path is otherwise untestable on a machine that has QGIS.**
      Pointing `CHESTER_QGIS_PROCESS_BIN`/`CHESTER_QGIS_APP` at nowhere does not
      help — discovery falls back to scanning `/Applications` and finds it anyway
      (measured 2026-09-06). Without this switch the only way to exercise the mode
      most users will run in is a monkeypatch inside a test.
    * **Phase KA needs both branches of the same machine.** An ablation that compares
      the QGIS path against the geopandas core has to switch between them without
      uninstalling anything.
    """
    env = os.environ.get("CHESTER_NO_QGIS", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    # `chester.json` ist der eigentliche Schalter; die Umgebungsvariable übersteuert
    # ihn nur, weil Phase KA beide Zweige derselben Maschine gegeneinander messen
    # will, ohne die Konfiguration zwischen zwei Läufen umzuschreiben.
    try:
        from chester.geoconfig import load_geodata

        return load_geodata()["use_qgis"] is False
    except Exception:  # noqa: BLE001 - eine unlesbare Konfiguration darf nichts abschalten
        return False


def qgis_available() -> bool:
    """Whether a usable ``qgis_process`` exists — askable without catching anything.

    Chester works without QGIS since 2026-09-06 (Phase KQ): the compute core is
    geopandas/rasterio/networkx and, for hydrology, GRASS. QGIS adds the 761-algorithm
    catalogue and the Desktop bridge, and `agent_build.geo_capabilities` leaves those
    capabilities out entirely when it is absent — a tool that cannot run is prompt
    cost, not a feature. Filtering happens at *capability* level so the instruction
    sections go with them.
    """
    if qgis_disabled():
        return False
    try:
        resolve_qgis_env()
    except QgisNotFoundError:
        return False
    return True


def find_gisbase() -> Path | None:
    """Locate the GRASS installation root, or ``None`` if there is none.

    QGIS advertises ~307 of its 761 algorithms under the ``grass:`` prefix, but runs
    none of them unless the GRASS provider finds a GISBASE. On macOS it never does:
    the bundled ``grass_utils.py`` searches ``/Applications/GRASS-7.{version}.app``
    with a hardcoded major version 7, so a GRASS 8 install is invisible to it no
    matter how it was installed. Setting the variable ourselves is not a workaround
    for a missing install — it is the only route that exists on this platform.

    Validity marker is ``etc/VERSIONNUMBER``, the GRASS counterpart to ``proj.db``:
    a bundle without it is a leftover directory, not a usable GISBASE.
    """
    candidates: list[Path] = []

    env_app = os.environ.get("CHESTER_GRASS_APP")
    if env_app:
        candidates.append(Path(env_app) / "Contents" / "Resources")
        candidates.append(Path(env_app))

    # Newest bundle first, so GRASS-8.4 wins over a leftover GRASS-7.8.
    for app in sorted(glob.glob("/Applications/GRASS*.app"), reverse=True):
        candidates.append(Path(app) / "Contents" / "Resources")

    # Linux / conda layouts
    candidates.append(Path("/usr/lib/grass84"))
    candidates.append(Path("/usr/local/grass84"))

    return next((c for c in candidates if (c / "etc" / "VERSIONNUMBER").exists()), None)


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
            "QGIS is switched off — either `geodata.use_qgis: false` in "
            "`.chester/chester.json` or CHESTER_NO_QGIS in the environment."
            if qgis_disabled() else
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

    gisbase = find_gisbase()
    if gisbase is not None:
        env["GISBASE"] = str(gisbase)
        # The provider resolves module binaries against PATH, not against GISBASE.
        env["PATH"] = os.pathsep.join(
            [str(gisbase / "bin"), str(gisbase / "scripts"), os.environ.get("PATH", "")]
        )

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
    plugins: str | None  # the built-in *Python* plugins dir (holds `processing`)
    providers: str | None  # the C++ provider dir (postgres, wms, wfs, spatialite …)
    pkgdata: str | None  # the package data dir (svg library, resources, srs.db)
    env: dict[str, str]

    def subprocess_env(self) -> dict[str, str]:
        merged = dict(os.environ)
        merged.update(self.env)
        return merged


def resolve_qgis_python_env() -> QgisPythonEnv:  # noqa: C901
# C901-Ausnahme: Kaskade von Kandidatenpfaden ueber macOS-Bundle/Prefix/Linux; das Aufteilen
# verteilte die Sondierung, ohne die Verzweigung zu verringern
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

    # ── the C++ *provider* dir (postgres, wms, wfs, spatialite, …) ───────
    # A different directory from the Python plugins above, and QGIS cannot derive
    # it: from the macOS prefix (`Contents/MacOS`) it computes
    # `<prefix>/Contents/PlugIns/qgis` — one `Contents` too many — finds nothing,
    # and registers only the 17 providers compiled into the core library. The
    # other 17, `postgres` among them, are simply absent, so a PyQGIS snippet
    # opening a PostGIS or WMS layer gets an invalid layer and no error at all
    # (measured 2026-08-19 against the ATKIS fixture: 17 of 34).
    providers: str | None = None
    for cand in (
        contents / "PlugIns" / "qgis",
        contents / "MacOS" / "lib" / "qgis" / "plugins",
        contents / "lib" / "qgis" / "plugins",
    ):
        if cand.is_dir():
            providers = str(cand)
            break

    # ── the package data dir (svg library, resources, srs.db) ───────────
    # Same derivation bug as the providers, one level further: from the macOS
    # prefix QGIS computes `<prefix>/Contents/Resources/qgis`, so the bundled SVG
    # library is never found and every SvgMarker renders as a "?" placeholder — a
    # map that draws, with its point symbols quietly replaced by question marks
    # (measured 2026-08-19 on the official ATKIS styles, 229 SvgMarker layers).
    pkgdata: str | None = None
    for cand in (contents / "Resources" / "qgis", contents / "share" / "qgis"):
        if (cand / "svg").is_dir():
            pkgdata = str(cand)
            break

    # ── PYTHONHOME (bundled stdlib) ──────────────────────────────────────
    for home in (contents / "Frameworks", contents):
        if any(home.glob("lib/python3.*")):
            env["PYTHONHOME"] = str(home)
            break

    prefix = contents / "MacOS"
    prefix_str = str(prefix) if prefix.is_dir() else str(contents)
    env["QGIS_PREFIX_PATH"] = prefix_str

    return QgisPythonEnv(bin=py_bin, prefix=prefix_str, plugins=plugins,
                         providers=providers, pkgdata=pkgdata, env=env)


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
