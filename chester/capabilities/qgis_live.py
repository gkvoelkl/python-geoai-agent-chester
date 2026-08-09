"""GeoLiveCapability — drive a live, interactive QGIS Desktop (roadmap step C).

This is the single mechanism for viewing data in QGIS Desktop: it launches QGIS
windowed via ``QGIS --code`` (which also starts the in-QGIS bridge — no plugin
install), then drives it over a local socket. If QGIS + the bridge are **already
running, it reuses them** — no second window. ``qgis_show`` is the agent-facing
tool (arbitrary layers); the ``/qgis`` slash command shares the *same* bridge path
for the last rendered map (see ``agent_build.register_geo_commands``).

Local-only and window-opening, so the instructions require the agent to **ask
first**. Uses only QGIS-bundled + stdlib libraries; no MCP, no extra dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import qgis_live_client as live
from chester.workspace import DEFAULT_WORKSPACE, resolve_path


def _looks_like_cityjson(path: str) -> bool:
    """True if ``path`` is a CityJSON file (by extension or a content sniff)."""
    if path.lower().endswith(".city.json"):
        return True
    if not path.lower().endswith(".json"):
        return False
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            head = f.read(2000)
        return '"CityJSON"' in head or '"CityObjects"' in head
    except OSError:
        return False

_INSTRUCTIONS = """\
## Live QGIS Desktop (interactive)

`qgis_show` opens layer(s) in a LIVE, interactive QGIS Desktop window that stays
under Chester's control (full features, attribute table, native styling). Because
it opens a window on the user's machine (local only), **always ask first** — e.g.
"Möchtest du das interaktiv in QGIS Desktop ansehen?" Only once the user confirms,
call `qgis_show` with the layer path(s).

For **point clouds** (LiDAR), use `qgis_show_pointcloud` — COPC (`.copc.laz`) or EPT
load natively into a 3D view (plain LAS/LAZ must be converted to COPC first; e.g.
swissSURFACE3D ships COPC directly).

For **3D building models / CityJSON**, use `qgis_show_3d` instead — it converts a
CityJSON to a MultiPolygonZ GeoPackage (real LoD2 ground/wall/roof shells, no plugin)
and opens a live 3D Map View with the layers Z-clamped. Same ask-first rule.

For an **OGC WMS service** (official basemaps, cadastre, zoning plans — rendered
images, not data), use `qgis_show_wms(url, layer)` — QGIS streams the service as a
native WMS raster layer (layer names via `wms_capabilities`). Same ask-first rule.

If QGIS and the bridge are already running, `qgis_show`/`qgis_show_3d` **reuse** them
(no second window). Afterwards you may call `qgis_screenshot` (returns a PNG path) or
`qgis_save_project` (writes a `.qgz` into the cache). Never call these unprompted.\
"""


@dataclass
class GeoLiveCapability(AbstractCapability[Any]):
    """Launch and drive a live QGIS Desktop session over the local bridge."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace

        def qgis_show(layers: list[str]) -> dict:
            """Open layer(s) in a LIVE, interactive QGIS Desktop window.

            Ask the user first (opens a window; local only). Launches a windowed
            QGIS if none is running, otherwise REUSES the running one (no second
            window), then loads the layers and zooms to them.
            """
            if not layers:
                return {"ok": False, "error": "no layers given"}
            resolved = [str(Path(resolve_path(p, ws)).resolve()) for p in layers]
            existing = [p for p in resolved if Path(p).exists()]
            if not existing:
                return {"ok": False, "error": "none of the given layers exist on disk"}
            # Convert big/mixed GeoJSON to a cached GeoPackage first (fast to load,
            # all geometries visible); non-GeoJSON passes through.
            loadable = [live.to_loadable(p) for p in existing]
            try:
                state = live.ensure_running()
                added = live._call("add_layers", paths=loadable, timeout=120.0)
                live._call("zoom_full")
            except live.QgisBridgeError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "qgis": state,
                    "added": added.get("added"), "failed": added.get("failed")}

        def qgis_show_wms(url: str, layer: str, name: str = "") -> dict:
            """Open an OGC WMS layer in a LIVE QGIS Desktop window (streamed).

            ``url`` is the WMS service endpoint, ``layer`` a layer name from
            ``wms_capabilities``. QGIS loads the service as a native WMS raster
            layer and re-requests tiles on pan/zoom — the full interactive way to
            view a WMS (display only: a WMS serves rendered images, not data).
            Ask the user first (opens a window; local only). Reuses a running QGIS.
            """
            if not url or not layer:
                return {"ok": False, "error": "url and layer are required"}
            try:
                state = live.ensure_running()
                added = live._call("add_wms", url=url, layer=layer,
                                   name=name or None, timeout=60.0)
            except live.QgisBridgeError as exc:
                msg = str(exc)
                if "unknown command" in msg:
                    msg += (" — the running QGIS was launched before WMS support; "
                            "close QGIS and call this tool again")
                return {"ok": False, "error": msg}
            if added.get("error"):
                return {"ok": False, "error": added["error"]}
            return {"ok": True, "qgis": state, "added": added.get("added")}

        def qgis_show_3d(layers: list[str]) -> dict:
            """Open layer(s) in a live QGIS **3D** Map View (real building shells).

            A **CityJSON** layer is converted to a MultiPolygonZ GeoPackage first
            (its LoD2 ground/wall/roof surfaces → 3D faces QGIS renders natively, no
            plugin); a layer that already has Z is shown as-is. Sets a 3D renderer
            (Z-clamped) on the polygon layers and opens a 3D view. Ask the user first
            (opens a window; local only). Use this for CityJSON / 3D building models.
            """
            if not layers:
                return {"ok": False, "error": "no layers given"}
            from chester import citymodel

            loadable = []
            for p in layers:
                rp = str(Path(resolve_path(p, ws)).resolve())
                if not Path(rp).exists():
                    continue
                if _looks_like_cityjson(rp):
                    gpkg = str(Path(resolve_path(Path(p).stem + "_3d.gpkg", ws)).resolve())
                    r = citymodel.cityjson_to_gpkg_z(rp, gpkg)
                    if r.get("ok"):
                        loadable.append(gpkg)
                else:
                    loadable.append(live.to_loadable(rp))
            if not loadable:
                return {"ok": False, "error": "no loadable 3D layers "
                        "(missing files, or empty CityJSON)"}
            try:
                state = live.ensure_running()
                added = live._call("add_layers", paths=loadable, timeout=180.0)
                styled = live._call("show_3d")
                live._call("zoom_full")
            except live.QgisBridgeError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "qgis": state, "added": added.get("added"),
                    "styled_3d": styled.get("styled_3d"),
                    "view_opened": styled.get("view_opened")}

        def qgis_show_pointcloud(layers: list[str]) -> dict:
            """Open **point cloud(s)** in a live QGIS **3D** Map View.

            For LiDAR point clouds in **COPC** (`.copc.laz`) or **EPT** format — these
            load natively (this QGIS build has the `copc`/`ept` providers, not `pdal`, so
            a plain `.las`/`.laz` must be converted to COPC first). Sets a 3D point-cloud
            renderer and opens a 3D view. Ask the user first (opens a window; local only).
            Sources: swissSURFACE3D ships COPC directly; other LAZ needs COPC conversion.
            """
            if not layers:
                return {"ok": False, "error": "no layers given"}
            loadable = []
            for p in layers:
                rp = str(Path(resolve_path(p, ws)).resolve())
                if Path(rp).exists():
                    loadable.append(rp)
            if not loadable:
                return {"ok": False, "error": "no point-cloud files found on disk"}
            try:
                state = live.ensure_running()
                shown = live._call("show_pointcloud", paths=loadable, timeout=180.0)
                live._call("zoom_full")
            except live.QgisBridgeError as exc:
                return {"ok": False, "error": str(exc)}
            if shown.get("failed") and not shown.get("added"):
                return {"ok": False, "error": "point cloud(s) not loadable — COPC/EPT "
                        "only on this QGIS (convert plain LAS/LAZ to COPC first)",
                        "failed": shown.get("failed")}
            return {"ok": True, "qgis": state, "added": shown.get("added"),
                    "failed": shown.get("failed"),
                    "view_opened": shown.get("view_opened")}

        def qgis_screenshot(path: str) -> dict:
            """Save a PNG screenshot of the live QGIS canvas (needs a window).

            Call `qgis_show` first. Returns the output path.
            """
            out = str(Path(resolve_path(path, ws)).resolve())
            if not live.is_running():
                return {"ok": False,
                        "error": "QGIS live bridge not running — call qgis_show first"}
            try:
                result = live._call("screenshot", path=out)
            except live.QgisBridgeError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": bool(result.get("exists")), "output": result.get("path")}

        def qgis_save_project(path: str) -> dict:
            """Save the live QGIS project as a `.qgz` in the cache. Call qgis_show first."""
            if not path.endswith((".qgz", ".qgs")):
                path += ".qgz"
            out = str(Path(resolve_path(path, ws)).resolve())
            if not live.is_running():
                return {"ok": False,
                        "error": "QGIS live bridge not running — call qgis_show first"}
            try:
                result = live._call("save_project", path=out)
            except live.QgisBridgeError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": bool(result.get("ok")), "output": result.get("path")}

        return FunctionToolset(
            tools=[qgis_show, qgis_show_wms, qgis_show_3d, qgis_show_pointcloud, qgis_screenshot,
                   qgis_save_project]
        )
