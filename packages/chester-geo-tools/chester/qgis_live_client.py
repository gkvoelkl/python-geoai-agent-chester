"""Chester-side client for the QGIS live bridge (``chester/qgis_bridge.py``).

Talks line-delimited JSON over TCP — stdlib ``socket``/``json`` only, no new
dependency. Key entry point is :func:`ensure_running`, which **reuses an already
running QGIS + bridge** and only launches a fresh (windowed) QGIS when none is
there — so calling ``qgis_show`` twice never opens a second window.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from chester.qgis_env import QgisNotFoundError, resolve_qgis_env

HOST = "127.0.0.1"
PORT = 9878
LAUNCH_TIMEOUT = 90  # seconds to wait for a freshly launched QGIS bridge

_STARTUP = Path(__file__).resolve().parent / "qgis_startup.py"


class QgisBridgeError(RuntimeError):
    """The bridge returned an error, was unreachable, or QGIS could not launch."""


def _call(cmd_type: str, timeout: float = 15.0, **params):
    """Send one command, return its ``result`` (or raise QgisBridgeError)."""
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(
                (json.dumps({"type": cmd_type, "params": params}) + "\n").encode("utf-8")
            )
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
    except OSError as exc:
        raise QgisBridgeError(f"QGIS bridge unreachable at {HOST}:{PORT} ({exc})") from exc

    line = bytes(buf).split(b"\n", 1)[0]
    if not line:
        raise QgisBridgeError("empty response from bridge")
    resp = json.loads(line.decode("utf-8"))
    if resp.get("status") != "success":
        raise QgisBridgeError(resp.get("message", "unknown bridge error"))
    return resp.get("result")


def to_loadable(path: str) -> str:  # noqa: C901
# C901-Ausnahme: Formatweichen (GeoJSON/GPKG/Geometrietypen) vor dem Laden in QGIS
    """Return a QGIS-friendly path for ``path``, converting a GeoJSON to a cached
    GeoPackage first.

    A large OSM GeoJSON is slow for QGIS to open (the OGR GeoJSON driver re-reads
    the whole file on every access — a mixed-geometry building layer can be
    hundreds of MB) and, being mixed-geometry, only *one* geometry type shows.
    Converting once to a GeoPackage — split by geometry type, indexed — fixes both:
    QGIS loads it instantly and every geometry renders. Cached next to the source
    and reused while newer than it. Runs Chester-side (venv), so it never blocks
    QGIS. Any failure falls back to the original path.
    """
    if not path.lower().endswith((".geojson", ".json")):
        return path
    gpkg = path.rsplit(".", 1)[0] + ".gpkg"
    try:
        if os.path.exists(gpkg) and os.path.getmtime(gpkg) >= os.path.getmtime(path):
            return gpkg
        import geopandas as gpd

        gdf = gpd.read_file(path)
        # GPKG/SQLite column names are case-insensitive → drop later duplicates
        # (OSM data has e.g. both `FIXME` and `fixme`, which collide in a GeoPackage).
        seen: set = set()
        keep: list = []
        for col in gdf.columns:
            low = col.lower()
            if low in seen and col != gdf.geometry.name:
                continue
            seen.add(low)
            keep.append(col)
        gdf = gdf[keep]
        # Promote single → multi so polygons/lines land in one layer each (not a
        # separate Polygon and MultiPolygon layer); points stay points.
        from shapely.geometry import MultiLineString, MultiPolygon

        def _promote(geom):
            if geom is None:
                return geom
            if geom.geom_type == "Polygon":
                return MultiPolygon([geom])
            if geom.geom_type == "LineString":
                return MultiLineString([geom])
            return geom

        gdf["geometry"] = gdf.geometry.apply(_promote)
        tmp = gpkg + ".tmp.gpkg"
        if os.path.exists(tmp):
            os.remove(tmp)
        for geom_type, sub in gdf.groupby(gdf.geom_type):
            sub.to_file(tmp, layer=str(geom_type), driver="GPKG")
        os.replace(tmp, gpkg)  # atomic: a partial conversion never becomes the cache
        return gpkg
    except Exception:  # noqa: BLE001 — any conversion issue → just load the original
        return path


def is_running() -> bool:
    """True if a QGIS live bridge is already reachable."""
    try:
        _call("ping", timeout=2.0)
        return True
    except QgisBridgeError:
        return False


def _gui_binary() -> str:
    """Path/command to launch the QGIS **Desktop** GUI (not qgis_process)."""
    env = resolve_qgis_env()  # raises QgisNotFoundError
    if sys.platform == "darwin":
        app = next((p for p in env.bin.parents if p.suffix == ".app"), None)
        return str(env.bin.parent / app.stem) if app else "qgis"
    cand = env.bin.parent / "qgis"
    return str(cand) if cand.exists() else "qgis"


def launch_qgis() -> None:
    """Launch a *windowed* QGIS whose ``--code`` startup starts the bridge.

    Detached (fire-and-forget). Uses a clean on-screen environment — the headless
    ``QT_QPA_PLATFORM=offscreen`` from ``resolve_qgis_env`` is removed so a window
    actually appears (and screenshots work).

    ``start_new_session=True`` puts QGIS in its **own session/process group** and
    all three std streams go to ``/dev/null``, so it shares no controlling
    terminal or process group with the gateway / ``start.sh``. Without this, the
    long-lived GUI keeps the launching shell's group alive and ``start.sh`` hangs
    on Ctrl-C. QGIS then lives independently of Chester's lifecycle (the user
    closes its window themselves).
    """
    env = resolve_qgis_env()  # raises QgisNotFoundError
    e = env.subprocess_env()
    e.pop("QT_QPA_PLATFORM", None)  # visible window
    e["CHESTER_BRIDGE_DIR"] = str(_STARTUP.parent)
    subprocess.Popen(
        [_gui_binary(), "--code", str(_STARTUP)],
        env=e,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure_running(timeout: int = LAUNCH_TIMEOUT) -> str:
    """Reuse a running bridge, else launch QGIS and wait for it.

    Returns ``"reused"`` if a bridge was already up, ``"launched"`` if a fresh
    QGIS was started. Raises QgisBridgeError (incl. QGIS-not-found) on failure.
    """
    if is_running():
        return "reused"
    try:
        launch_qgis()
    except QgisNotFoundError as exc:
        raise QgisBridgeError(f"QGIS not found: {exc}") from exc
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running():
            return "launched"
        time.sleep(1)
    raise QgisBridgeError(f"QGIS bridge did not come up within {timeout}s")
