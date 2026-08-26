"""GeoCityModelCapability — 3D building models (CityJSON) and their 3D display.

The agent surface over ``chester/citymodel.py`` (the pure CityGML→CityJSON writer +
cjio reader + renderers). Turns the LoD2 building models (measured 3D geometry) into
CityJSON and into interactive 3D output:

- ``fetch_cityjson(bbox, output_path, state?)`` — fetch the covering open LoD2
  CityGML tiles (Bayern/NRW/Brandenburg/M-V), write CityJSON, clip to the bbox.
- ``fetch_swissbuildings3d(bbox, output_path)`` — the Swiss counterpart:
  swissBUILDINGS3D 3.0 (swisstopo, EPSG:2056) → CityJSON.
- ``render_buildings_3d(cityjson_path, output_path, style?)`` — CityJSON → a
  self-contained 3D HTML: ``"roofs"`` (three.js, real LoD2 shells) or ``"blocks"``
  (MapLibre 2.5D extrusion).
- ``cityjson_to_geopackage(cityjson_path, output_path)`` — CityJSON → a
  MultiPolygonZ GeoPackage (QGIS-native 3D; also what ``qgis_show_3d`` uses).

For a live QGIS 3D view, pass the CityJSON straight to ``qgis_show_3d``
(GeoLiveCapability). No Java anywhere — Chester writes the CityJSON itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import citymodel, lod2, provenance, swisstopo
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

# What each 3D page still fetches *when it is opened*. Both viewers pull their JS
# library from a CDN — the data is embedded, the library is not — and the MapLibre
# page streams its basemap live on top, where the three.js page bakes its ground
# plate in at render time. Chester is otherwise a local agent, so a viewer that
# quietly needs the internet is worth stating. Measured 2026-08-19 by pointing both
# generated pages at a dead host: each comes up as an **empty page** — grey for the
# three.js viewer, white for MapLibre — with the failure visible *only* as a
# `ReferenceError: THREE/maplibregl is not defined` in the browser console. Nothing
# on the page, nothing in the tool's return value, and the agent never sees a
# console. So the return value has to carry it.
_VIEWER_HOSTS_ROOFS = ["unpkg.com (three.js)"]
_VIEWER_HOSTS_BLOCKS = [
    "unpkg.com (maplibre-gl)",
    "a.tile.openstreetmap.org (basemap tiles)",
]

_INSTRUCTIONS = """\
## 3D building models (CityJSON) + 3D display

For **3D buildings** — a 3D web view, real roof shapes, a CityJSON model — use the
open LoD2 models (measured 3D geometry per building), not `building:levels`:

1. Fetch the buildings as a CityJSON, by country:
   - **Germany**: `fetch_cityjson(bbox, output_path)` — the covering open LoD2
     (Bayern/NRW/Brandenburg/M-V auto-detected).
   - **Switzerland**: `fetch_swissbuildings3d(bbox, output_path)` — swissBUILDINGS3D
     3.0 (swisstopo, EPSG:2056), the Swiss equivalent.
   - **Austria/Vienna**: `fetch_vienna_buildings(output_path, source=…)` — Vienna's open
     LOD2.1 CityGML (EPSG:31256). `source="sample"` grabs the public demo tile; for other
     areas pass a local `.gml`/`.zip` downloaded from Vienna's OGD portal (no clean
     per-bbox URL). No national AT building model exists.
   Either way, **keep the bbox tight** — the same small area you analysed (a street/
   block/100 m radius), not a whole district: the web 3D view inlines the geometry,
   so thousands of buildings won't embed.
2. Then display:
   - **Web**: `render_buildings_3d(cityjson, output.html, style="roofs")` — an
     interactive three.js page with the real LoD2 shells; `style="blocks"` for a
     lighter MapLibre 2.5D extrusion. Report the HTML path (the dashboard embeds it).
     If the result is `ok: false` with `embedded: false` (model too large to inline),
     **no file was written** — do not paste a path or describe the model; tell the
     user it is too big and offer `qgis_show_3d` instead.
   - **QGIS**: `qgis_show_3d(cityjson)` — a live QGIS 3D Map View (ask the user
     first; it opens a window). Prefer this for large areas.
   - **GIS file**: `cityjson_to_geopackage(cityjson, out.gpkg)` — a MultiPolygonZ
     layer for further QGIS work.

The heights are laser-measured (LoD2), so a building's height is exact — read it from
the buildings' `measured_height`. Cite the source licence in your answer.\
"""


@dataclass
class GeoCityModelCapability(AbstractCapability[Any]):
    """Fetch/build CityJSON 3D building models and render them in 3D."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace

        def fetch_cityjson(
            bbox: list[float],
            output_path: str,
            state: str | None = None,
        ) -> dict:
            """Fetch open LoD2 buildings for a bbox and write a **CityJSON** model.

            ``bbox`` = [west, south, east, north] in WGS84. Downloads the covering
            open LoD2 CityGML tiles (Bayern/NRW/Brandenburg/M-V; ``state`` forces
            one), converts them to CityJSON (pure Python, no Java), and clips to the
            bbox. The output has the real LoD2 3D geometry (ground/wall/roof
            surfaces) + `measuredHeight` per building. Feed it to
            `render_buildings_3d`, `qgis_show_3d`, or `cityjson_to_geopackage`.
            """
            output_path = str(resolve_path(output_path, ws))
            tile_cache = str(resolve_path("_lod2_tiles", ws))
            dl = lod2.download_citygml_tiles(bbox, tile_cache, state=state)
            if not dl.get("ok"):
                return dl
            full = str(resolve_path("_cityjson_full.json", ws))
            try:
                citymodel.write_cityjson(dl["gml_paths"], full, epsg=dl["epsg"])
                r = citymodel.subset_bbox(full, output_path, bbox, epsg=dl["epsg"])
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if not r.get("ok"):
                return {**r, "state": dl["state"]}
            provenance.write_meta(
                output_path, source=f"connector/lod2-{dl['state'].lower()}",
                tool="fetch_cityjson", query={"bbox": bbox, "state": dl["state"]},
                crs=r.get("crs"), licence=dl.get("licence"))
            return {"ok": True, "output": output_path, "state": dl["state"],
                    "state_name": dl.get("state_name"), "buildings": r["buildings"],
                    "crs": r.get("crs"), "licence": dl.get("licence")}

        def fetch_swissbuildings3d(
            bbox: list[float],
            output_path: str,
        ) -> dict:
            """Fetch **Swiss** 3D buildings (swissBUILDINGS3D 3.0) as a **CityJSON** model.

            ``bbox`` = [west, south, east, north] in WGS84 (**Switzerland only**). The
            Swiss counterpart of `fetch_cityjson`: downloads the covering
            swissBUILDINGS3D 3.0 tiles (swisstopo, EPSG:2056), reads the 3D building
            solids and writes a CityJSON model with a `measured_height` per building.
            **Keep the bbox tight** (a street/block/100 m radius) — the 3D geometry
            inlines. Feed the result to `render_buildings_3d`, `qgis_show_3d`, or
            `cityjson_to_geopackage`. For German buildings use `fetch_cityjson` instead.
            """
            output_path = str(resolve_path(output_path, ws))
            tile_cache = str(resolve_path("_swissbuildings3d_tiles", ws))
            try:
                r = swisstopo.fetch_swissbuildings3d(bbox, output_path, tile_cache)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if not r.get("ok"):
                return r
            provenance.write_meta(
                output_path, source="connector/swissbuildings3d",
                tool="fetch_swissbuildings3d", query={"bbox": bbox},
                crs=r.get("crs"), licence=r.get("licence"))
            return r

        def fetch_vienna_buildings(
            output_path: str,
            source: str = "sample",
            bbox: list[float] | None = None,
        ) -> dict:
            """Convert **Vienna's** open LOD2.1 CityGML roof model to a **CityJSON** model.

            ``source`` = ``"sample"`` (Vienna's public demo tile, downloaded) **or a local
            path** to a CityGML `.gml` / a `.zip` of tiles you downloaded from Vienna's
            OGD portal (`www.wien.gv.at/stadtplanung/generalisiertes-dachmodell`) — Vienna
            ships the whole city per-tile through that portal, so pass the tile(s) you
            fetched. Writes a CityJSON model in **EPSG:31256** (optionally clipped to
            ``bbox`` [w,s,e,n] WGS84). Feed the result to `render_buildings_3d`,
            `qgis_show_3d`, or `cityjson_to_geopackage`. For German buildings use
            `fetch_cityjson`, for Swiss `fetch_swissbuildings3d`.
            """
            from chester import austria

            if not output_path.endswith(".json"):
                output_path += ".city.json"
            out = str(resolve_path(output_path, ws))
            cache_dir = str(resolve_path("_vienna_lod2", ws))
            # a local source path is resolved; the "sample" keyword passes through
            src = source if source == "sample" else str(resolve_path(source, ws))
            try:
                r = austria.fetch_vienna_buildings(out, cache_dir, source=src,
                                                   bbox_wgs84=bbox)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    out, source="connector/vienna-lod2", tool="fetch_vienna_buildings",
                    query={"source": source, "bbox": bbox},
                    crs=r.get("crs"), licence=r.get("licence"))
            return r

        def render_buildings_3d(cityjson_path: str | None, output_path: str,
                                style: str = "roofs", basemap: bool = True,
                                relief: bool = False, pointcloud: str | None = None,
                                pointcloud_epsg: int | None = None) -> dict:
            """Render LoD2 buildings and/or a **LiDAR point cloud** to a 3D HTML page.

            ``style="roofs"`` (default) = a three.js viewer with the real LoD2 shells on
            an OSM ground plate (`basemap`); ``style="blocks"`` = a lighter MapLibre 2.5D
            extrusion (buildings only). Pass ``pointcloud`` (a LAS/LAZ/**COPC** path) to
            **overlay a point cloud** in the same three.js scene — decimated, coloured by
            LAS classification, reprojected to the buildings' CRS and aligned. Either
            input may be omitted: buildings-only, points-only (pass only ``pointcloud``,
            with ``cityjson_path`` empty), or both. ``pointcloud_epsg`` sets the cloud's
            CRS if its file lacks one. With ``relief`` the ground plate is a DGM1 terrain
            mesh. Writes standalone HTML; report the path so the dashboard embeds it. If
            `ok: false` with `embedded: false`, the scene is too big and NO file was
            written — narrow it or use QGIS.

            **Both pages load their JS library from unpkg.com when opened**, and
            ``"blocks"`` additionally streams OSM tiles live; ``"roofs"`` bakes its
            ground plate in at render time. The returned ``needs_online`` says which
            hosts. Without them the page opens **empty**, and the only trace is a
            console error nobody sees — so on a machine without internet prefer
            ``"roofs"`` (one host) or `qgis_show_3d` (none), and tell the user the
            page needs those hosts instead of promising a working view.
            """
            if not output_path.endswith(".html"):
                output_path += ".html"
            out = str(resolve_path(output_path, ws))
            src = None
            if cityjson_path:
                src = str(resolve_path(cityjson_path, ws))
                if not Path(src).exists():
                    return {"ok": False, "error": f"no such CityJSON: {cityjson_path}"}
            pc = str(resolve_path(pointcloud, ws)) if pointcloud else None
            if pc and not Path(pc).exists():
                return {"ok": False, "error": f"no such point cloud: {pointcloud}"}
            if not src and not pc:
                return {"ok": False, "error": "pass a CityJSON, a point cloud, or both"}
            # `blocks` only actually applies to a buildings-only render; a point
            # cloud always goes through the three.js viewer. The note below must
            # describe the renderer that *ran*, not the one that was asked for.
            # Keep `src` in the condition rather than folding it into a bool: the
            # MapLibre renderer takes a plain `str`, and a boolean flag hides that
            # narrowing from the type checker.
            as_blocks = style == "blocks" and src is not None and not pc
            try:
                if as_blocks and src is not None:
                    r = citymodel.render_cityjson_html(src, out)
                else:
                    r = citymodel.render_cityjson_html_3d(
                        src, out, basemap=basemap, relief=relief,
                        pointcloud=pc, pointcloud_epsg=pointcloud_epsg)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                r["needs_online"] = _VIEWER_HOSTS_BLOCKS if as_blocks else _VIEWER_HOSTS_ROOFS
                r["offline_note"] = (
                    "opening this page fetches the hosts above. Without them it "
                    "renders as an EMPTY page — the only trace is a ReferenceError "
                    "in the browser console, nothing on the page and nothing here"
                )
            return r

        def cityjson_to_geopackage(cityjson_path: str, output_path: str) -> dict:
            """Convert a CityJSON to a **MultiPolygonZ GeoPackage** (QGIS-native 3D).

            Each building's ground/wall/roof surfaces become 3D faces of one
            MultiPolygon Z feature — QGIS renders it in its 3D view with no plugin.
            """
            src = str(resolve_path(cityjson_path, ws))
            if not Path(src).exists():
                return {"ok": False, "error": f"no such CityJSON: {cityjson_path}"}
            if not output_path.endswith(".gpkg"):
                output_path += ".gpkg"
            out = str(resolve_path(output_path, ws))
            try:
                r = citymodel.cityjson_to_gpkg_z(src, out)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(out, source="chester",
                                      tool="cityjson_to_geopackage",
                                      query={"from": cityjson_path}, crs=r.get("crs"))
            return r

        return FunctionToolset(
            tools=[fetch_cityjson, fetch_swissbuildings3d, fetch_vienna_buildings,
                   render_buildings_3d, cityjson_to_geopackage])
