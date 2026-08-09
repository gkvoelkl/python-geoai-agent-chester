"""Chester's QGIS live bridge — runs INSIDE QGIS (via ``QGIS --code``).

A minimal line-delimited-JSON TCP server that lets Chester drive a *running*
QGIS Desktop. It uses only QGIS-bundled libraries (``QtNetwork`` + Python stdlib
+ PyQGIS) — **no external dependency, no MCP, no plugin install**. It is started
by ``chester/qgis_startup.py`` (the ``--code`` entry), not imported into Chester's
own venv (which runs a different Python).

Concurrency: ``QTcpServer`` fires ``newConnection`` / ``readyRead`` as Qt signals
on QGIS's **main thread**, so every handler runs where PyQGIS is safe — no worker
threads, no polling, no marshalling.

Protocol: request ``{"type": <cmd>, "params": {...}}\\n`` → response
``{"status": "success", "result": {...}}\\n`` or
``{"status": "error", "message": ...}\\n``.
"""

from __future__ import annotations

import json
import os
import traceback

from qgis.PyQt.QtCore import QObject
from qgis.PyQt.QtNetwork import QHostAddress, QTcpServer
from qgis.core import (
    Qgis,
    QgsMessageLog,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsWkbTypes,
)

HOST = "127.0.0.1"
PORT = 9878
_TAG = "ChesterQgisLive"
# OSM XYZ basemap datasource ({z}/{x}/{y} are percent-encoded for the QGIS URI).
_OSM_BASEMAP = (
    "type=xyz&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png"
    "&zmax=19&zmin=0"
)


class LiveBridge(QObject):
    """A line-delimited-JSON TCP server running inside QGIS's event loop."""

    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._on_new_connection)
        self._buffers: dict = {}  # QTcpSocket -> bytearray

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if not self.server.listen(QHostAddress(HOST), PORT):
            raise RuntimeError(
                f"QTcpServer.listen({HOST}:{PORT}) failed: {self.server.errorString()}"
            )
        QgsMessageLog.logMessage(f"listening on {HOST}:{PORT}", _TAG)

    def stop(self) -> None:
        for sock in list(self._buffers):
            sock.close()
        self._buffers.clear()
        self.server.close()

    # -- socket plumbing (all on the main thread) ----------------------------
    def _on_new_connection(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self._buffers[sock] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._cleanup(s))

    def _on_ready_read(self, sock) -> None:
        self._buffers[sock].extend(bytes(sock.readAll()))
        while b"\n" in self._buffers[sock]:
            line, _, rest = self._buffers[sock].partition(b"\n")
            self._buffers[sock] = bytearray(rest)
            self._handle_line(sock, bytes(line))

    def _cleanup(self, sock) -> None:
        self._buffers.pop(sock, None)
        sock.deleteLater()

    def _send(self, sock, obj: dict) -> None:
        sock.write((json.dumps(obj) + "\n").encode("utf-8"))
        sock.flush()

    def _handle_line(self, sock, line: bytes) -> None:
        try:
            command = json.loads(line.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._send(sock, {"status": "error", "message": f"bad JSON: {exc}"})
            return
        self._send(sock, self._dispatch(command))

    def _dispatch(self, command: dict) -> dict:
        handlers = {
            "ping": self._ping,
            "add_layers": self._add_layers,
            "add_wms": self._add_wms,
            "show_3d": self._show_3d,
            "show_pointcloud": self._show_pointcloud,
            "save_project": self._save_project,
            "zoom_full": self._zoom_full,
            "screenshot": self._screenshot,
        }
        handler = handlers.get(command.get("type"))
        if handler is None:
            return {"status": "error",
                    "message": f"unknown command {command.get('type')!r}"}
        try:
            return {"status": "success",
                    "result": handler(**(command.get("params") or {}))}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "trace": traceback.format_exc()}

    # -- command handlers (main thread) --------------------------------------
    def _ping(self) -> dict:
        proj = QgsProject.instance()
        return {"qgis_version": Qgis.QGIS_VERSION,
                "project": proj.fileName() or None,
                "layer_count": len(proj.mapLayers())}

    def _ensure_basemap(self) -> None:
        """Add an OSM background map once (bottom layer), if not already present."""
        proj = QgsProject.instance()
        if any(lyr.name() == "OpenStreetMap" for lyr in proj.mapLayers().values()):
            return
        osm = QgsRasterLayer(_OSM_BASEMAP, "OpenStreetMap", "wms")
        if osm.isValid():
            # Added first, so later data layers stack on top of it.
            proj.addMapLayer(osm)

    def _add_layers(self, paths: list) -> dict:
        proj = QgsProject.instance()
        self._ensure_basemap()
        added, failed = [], []
        for path in paths:
            base = os.path.basename(path)
            # querySublayers with ResolveGeometryType splits a mixed-geometry file
            # (e.g. OSM buildings: Point + Polygon + MultiPolygon) into one layer
            # per geometry type — otherwise OGR loads only one type and the rest
            # (usually the polygons) silently vanish. Also expands multi-layer
            # containers (GeoPackage/SpatiaLite).
            subs = QgsProviderRegistry.instance().querySublayers(
                path, Qgis.SublayerQueryFlag.ResolveGeometryType
            )
            loaded = False
            for sub in subs:
                geom = QgsWkbTypes.geometryDisplayString(
                    QgsWkbTypes.geometryType(sub.wkbType())
                )
                name = f"{base} ({geom})" if len(subs) > 1 and geom else base
                layer = QgsVectorLayer(sub.uri(), name, sub.providerKey())
                if not layer.isValid():
                    layer = QgsRasterLayer(sub.uri(), name, sub.providerKey())
                if layer.isValid():
                    proj.addMapLayer(layer)
                    added.append(layer.name())
                    loaded = True
            if not loaded:  # fallback: load the file directly
                layer = QgsVectorLayer(path, base, "ogr")
                if not layer.isValid():
                    layer = QgsRasterLayer(path, base)
                if layer.isValid():
                    proj.addMapLayer(layer)
                    added.append(base)
                    loaded = True
            if not loaded:
                failed.append(path)
        if added:
            self._zoom_to_data()
            self.iface.mapCanvas().refresh()
        return {"added": added, "failed": failed}

    def _zoom_to_data(self) -> None:
        """Zoom to the combined extent of the *data* layers, not the whole world.

        ``zoomToFullExtent`` includes the global OSM basemap, so it always zooms
        out to the planet. Instead union the extents of the non-basemap layers
        (reprojected to the canvas CRS) and zoom there; fall back to full extent
        when only the basemap is present.
        """
        from qgis.core import QgsCoordinateTransform, QgsRectangle

        proj = QgsProject.instance()
        canvas = self.iface.mapCanvas()
        dest = proj.crs()
        combined = QgsRectangle()
        combined.setMinimal()
        for lyr in proj.mapLayers().values():
            if lyr.name() == "OpenStreetMap":  # the global basemap
                continue
            ext = lyr.extent()
            if ext.isEmpty():
                continue
            try:
                ext = QgsCoordinateTransform(lyr.crs(), dest, proj).transformBoundingBox(ext)
            except Exception:  # noqa: BLE001 - keep the untransformed extent
                pass
            combined.combineExtentWith(ext)
        if combined.isEmpty() or not combined.isFinite():
            canvas.zoomToFullExtent()
            return
        combined.scale(1.05)  # a little breathing room around the data
        canvas.setExtent(combined)

    def _add_wms(self, url: str, layer: str, name=None,
                 fmt: str = "image/png", crs: str = "EPSG:3857") -> dict:
        """Add an OGC WMS layer as a native QGIS raster layer (provider "wms").

        The service URL is percent-encoded into the provider URI so its own query
        params (``?map=…``) don't fight the URI's ``&``-separated keys. Zooms to
        the layer's extent (a WMS has no features to zoom to via data).
        """
        from urllib.parse import quote

        proj = QgsProject.instance()
        self._ensure_basemap()
        uri = (f"crs={crs}&dpiMode=7&format={fmt}"
               f"&layers={quote(layer, safe='')}&styles="
               f"&url={quote(url, safe='')}")
        lyr = QgsRasterLayer(uri, name or f"WMS: {layer}", "wms")
        if not lyr.isValid():
            return {"added": [], "failed": [layer],
                    "error": "QGIS could not load the WMS layer (check url/layer name)"}
        proj.addMapLayer(lyr)
        canvas = self.iface.mapCanvas()
        try:  # zoom to the service-advertised extent, reprojected to the canvas CRS
            from qgis.core import QgsCoordinateTransform

            extent = QgsCoordinateTransform(
                lyr.crs(), proj.crs(), proj
            ).transformBoundingBox(lyr.extent())
            canvas.setExtent(extent)
        except Exception:  # noqa: BLE001 - zoom is cosmetic
            canvas.zoomToFullExtent()
        canvas.refresh()
        return {"added": [lyr.name()], "failed": []}

    def _show_3d(self, extrusion_height=None) -> dict:
        """Give the loaded polygon layers a 3D renderer and open a 3D map view.

        A layer with Z (a MultiPolygonZ from a CityJSON) clamps to its own geometry
        height (real LoD2 shells); a flat 2D layer is extruded by ``extrusion_height``
        if given. Opening the 3D view is best-effort (the renderer alone makes the
        layer show correctly once a 3D Map View is open).
        """
        from qgis._3d import (
            QgsPhongMaterialSettings,
            QgsPolygon3DSymbol,
            QgsVectorLayer3DRenderer,
        )
        from qgis.PyQt.QtGui import QColor

        styled = []
        for lyr in QgsProject.instance().mapLayers().values():
            if not isinstance(lyr, QgsVectorLayer):
                continue
            if QgsWkbTypes.geometryType(lyr.wkbType()) != Qgis.GeometryType.Polygon:
                continue
            if lyr.name() == "OpenStreetMap":
                continue
            symbol = QgsPolygon3DSymbol()
            material = QgsPhongMaterialSettings()
            material.setDiffuse(QColor("#b0b0b8"))
            symbol.setMaterialSettings(material)
            if QgsWkbTypes.hasZ(lyr.wkbType()):
                symbol.setAltitudeClamping(Qgis.AltitudeClamping.Absolute)
            elif extrusion_height:
                symbol.setExtrusionHeight(float(extrusion_height))
            lyr.setRenderer3D(QgsVectorLayer3DRenderer(symbol))
            styled.append(lyr.name())

        opened = False
        try:
            if hasattr(self.iface, "createNewMapCanvas3D"):
                self.iface.createNewMapCanvas3D(f"{_TAG} 3D")
                opened = True
        except Exception:  # noqa: BLE001 - renderer is set regardless of the view
            opened = False
        return {"styled_3d": styled, "view_opened": opened}

    def _show_pointcloud(self, paths) -> dict:
        """Load COPC/EPT point cloud(s) and open a 3D map view.

        This QGIS build exposes the ``copc`` / ``ept`` point-cloud providers (not
        ``pdal``), so a Cloud-Optimized Point Cloud (``.copc.laz``, local or a remote
        HTTP range-read URL) or an Entwine tileset loads natively; a plain ``.las``/
        ``.laz`` is reported as unsupported (convert to COPC first). QGIS assigns the
        point-cloud layer a default 3D renderer when it enters the 3D scene, so opening a
        3D Map View is enough — we don't force an empty renderer over it.
        """
        from qgis.core import QgsPointCloudLayer

        proj = QgsProject.instance()
        self._ensure_basemap()
        added, failed = [], []
        for path in paths:
            base = os.path.basename(path)
            provider = "ept" if path.lower().endswith(".json") else "copc"
            lyr = QgsPointCloudLayer(path, base, provider)
            if not lyr.isValid():
                failed.append(path)
                continue
            proj.addMapLayer(lyr)
            added.append(lyr.name())

        opened = False
        if added:
            self.iface.mapCanvas().zoomToFullExtent()
            self.iface.mapCanvas().refresh()
            try:
                if hasattr(self.iface, "createNewMapCanvas3D"):
                    self.iface.createNewMapCanvas3D(f"{_TAG} 3D")
                    opened = True
            except Exception:  # noqa: BLE001
                opened = False
        return {"added": added, "failed": failed, "view_opened": opened}

    def _save_project(self, path: str) -> dict:
        ok = QgsProject.instance().write(path)
        return {"path": path, "ok": bool(ok)}

    def _zoom_full(self) -> dict:
        # Zoom to the data layers (not the global basemap → not the whole world).
        self._zoom_to_data()
        return {"ok": True}

    def _screenshot(self, path: str) -> dict:
        # Requires a visible window (offscreen has no paint device).
        self.iface.mapCanvas().saveAsImage(path)
        return {"path": path, "exists": os.path.exists(path)}
