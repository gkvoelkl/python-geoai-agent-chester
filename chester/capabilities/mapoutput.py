"""MapOutputCapability — render results as an interactive HTML map.

The end of a geo workflow is usually "show me". This wraps GeoPandas' folium-based
``explore`` to write a standalone, interactive HTML map of one or more vector
layers, so Chester can hand the user something to look at.
folium/geopandas are imported lazily to keep startup fast.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import BinaryContent, RunContext, ToolReturn
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import provenance
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

# A few distinct colours for stacking layers.
_COLORS = ["#3388ff", "#e6550d", "#31a354", "#756bb1", "#d62728", "#17becf"]

# Guardrails against an inline web map that freezes the dashboard: above these a
# layer is too heavy to embed in the chat (a 490 MB HTML did exactly that) and is
# steered to QGIS Desktop (`qgis_show`) instead.
_MAX_INLINE_FEATURES = 50_000   # cheap pre-check, before any heavy read/render
_MAX_INLINE_MB = 45             # hard backstop on the produced HTML size

# Raster layers render as a Folium ImageOverlay (reprojected to WGS84) rather
# than through the vector reader. These extensions route a layer to that path.
_RASTER_EXTS = {
    ".tif", ".tiff", ".vrt", ".img", ".asc", ".jp2", ".hgt", ".dem", ".dt2", ".bil",
}
# Cap the overlay image's longest side — a full-res raster PNG would bloat the HTML.
_MAX_RASTER_PX = 2000


def _is_raster(path: str) -> bool:
    import os

    return os.path.splitext(path)[1].lower() in _RASTER_EXTS


def _raster_to_rgba(data, nodata, cmap: str):
    """Turn a (bands, H, W) float array into an (H, W, 4) uint8 RGBA image.

    Single-band → colourised with ``cmap`` over a 2–98 percentile stretch;
    3+ bands → an RGB composite (per-band stretch). NoData / non-finite pixels
    are made transparent.
    """
    import matplotlib
    import numpy as np

    if data.shape[0] >= 3:
        rgb = np.moveaxis(data[:3], 0, -1)
        out = np.zeros(rgb.shape[:2] + (4,), dtype="uint8")
        opaque = np.ones(rgb.shape[:2], dtype=bool)
        for c in range(3):
            band = rgb[..., c]
            finite = np.isfinite(band) & (band != nodata if nodata is not None else True)
            vals = band[finite]
            if vals.size:
                lo, hi = np.percentile(vals, [2, 98])
                out[..., c] = np.clip((band - lo) / (hi - lo + 1e-9) * 255, 0, 255)
            opaque &= finite
        out[..., 3] = np.where(opaque, 255, 0)
        return out

    band = data[0]
    mask = ~np.isfinite(band)
    if nodata is not None:
        mask |= band == nodata
    vals = band[~mask]
    vmin, vmax = (np.percentile(vals, [2, 98]) if vals.size else (0.0, 1.0))
    norm = (np.clip(band, vmin, vmax) - vmin) / (vmax - vmin + 1e-9)
    rgba = (matplotlib.colormaps[cmap](norm) * 255).astype("uint8")
    rgba[mask, 3] = 0
    return rgba


def _raster_rgba_and_bounds(resolved: str, cmap: str):
    """Read a raster (decimated if large), reproject to WGS84, and return
    ``(rgba_uint8, [[south, west], [north, east]])`` ready for an ImageOverlay."""
    import numpy as np
    import rasterio
    from rasterio.transform import array_bounds
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    with rasterio.open(resolved) as src:
        bands = min(src.count, 3)
        scale = max(src.width, src.height) / _MAX_RASTER_PX
        out_w = int(src.width / scale) if scale > 1 else src.width
        out_h = int(src.height / scale) if scale > 1 else src.height
        data = src.read(
            list(range(1, bands + 1)),
            out_shape=(bands, out_h, out_w),
            resampling=Resampling.average,
        ).astype("float32")
        nodata = src.nodata
        src_transform = src.transform * src.transform.scale(
            src.width / out_w, src.height / out_h
        )
        if src.crs and src.crs.to_epsg() != 4326:
            bounds = array_bounds(out_h, out_w, src_transform)
            transform, width, height = calculate_default_transform(
                src.crs, "EPSG:4326", out_w, out_h, *bounds
            )
            dst = np.full((bands, height, width), np.nan, dtype="float32")
            for b in range(bands):
                reproject(
                    source=data[b], destination=dst[b],
                    src_transform=src_transform, src_crs=src.crs,
                    dst_transform=transform, dst_crs="EPSG:4326",
                    src_nodata=nodata, dst_nodata=np.nan,
                    resampling=Resampling.nearest,
                )
            data, nodata = dst, np.nan
            west, south, east, north = array_bounds(height, width, transform)
        else:
            west, south, east, north = array_bounds(out_h, out_w, src_transform)

    return _raster_to_rgba(data, nodata, cmap), [[south, west], [north, east]]


def _render_snapshot(layers, ws, column, scheme, k, cmap, title):
    """Render layers to a static PNG (bytes) + a per-layer fact summary.

    Unlike ``render_map`` (interactive Folium HTML), this is a flat image a
    vision model can actually look at — the artefact behind visual validation.
    Everything is drawn in WGS84 so mixed layers align; a raster is shown as its
    colourised overlay, vectors are plotted (choropleth if ``column`` fits).
    """
    import io

    import matplotlib

    matplotlib.use("Agg")
    import geopandas as gpd
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 8))
    summary = []
    for i, path in enumerate(layers):
        resolved = resolve_path(path, ws)
        name = path.split("/")[-1]
        if _is_raster(path):
            rgba, ((south, west), (north, east)) = _raster_rgba_and_bounds(resolved, cmap)
            ax.imshow(rgba, extent=[west, east, south, north], origin="upper", zorder=i)
            summary.append({
                "layer": name, "type": "raster",
                "extent_wgs84": [round(west, 5), round(south, 5),
                                 round(east, 5), round(north, 5)],
            })
            continue

        gdf = gpd.read_file(resolved)
        if gdf.empty:
            summary.append({"layer": name, "type": "vector", "features": 0})
            continue
        crs = gdf.crs.to_string() if gdf.crs else None
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(4326)
        info = {
            "layer": name, "type": "vector", "features": len(gdf),
            "geometry_types": sorted({g.geom_type for g in gdf.geometry if g is not None}),
            "crs": crs,
            "extent_wgs84": [round(float(v), 5) for v in gdf.total_bounds],
        }
        if column and column in gdf.columns:
            use_k = max(1, min(k, int(gdf[column].nunique(dropna=True))))
            gdf.plot(ax=ax, column=column, scheme=scheme, k=use_k, cmap=cmap,
                     legend=True, edgecolor="grey", linewidth=0.3, zorder=i)
            vals = gdf[column].dropna()
            info["column"] = column
            info["value_range"] = ([float(vals.min()), float(vals.max())]
                                   if len(vals) else None)
        else:
            gdf.plot(ax=ax, color=_COLORS[i % len(_COLORS)], edgecolor="black",
                     linewidth=0.3, alpha=0.6, zorder=i)
        summary.append(info)

    # A basemap under the data makes geographic placement legible — without it a
    # vector snapshot is shapes on white, and an off-coast / wrong-CRS layer looks
    # fine. Best-effort: needs contextily + a network tile fetch, so any failure
    # (offline, import missing) just leaves the plain plot.
    if summary and any(s.get("type") == "vector" for s in summary):
        try:
            import contextily as cx

            cx.add_basemap(ax, crs="EPSG:4326", source=cx.providers.OpenStreetMap.Mapnik)
        except Exception:  # noqa: BLE001 - the basemap is a nicety, not required
            pass

    ax.set_title(title)
    ax.set_axis_off()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue(), summary


_DEFAULT_REVIEW_PROMPT = (
    "You are validating a GIS result. Describe what you see, then judge plausibility: "
    "is the data placed where it should be (not off-coast / wrong hemisphere ⇒ a CRS "
    "bug), is the extent sensible, do partition layers tile without gaps/overlaps, and "
    "does a choropleth's colour actually vary? Flag anything that looks wrong."
)


def _ask_vision_model(model_str: str, base_url: str, png: bytes, question) -> str:
    """Send the snapshot to a dedicated vision model and return its written verdict.

    The fallback for a text-only main model: build the configured vision model with
    SelmaKit's own dispatch (so any provider works) and run one multimodal turn.
    """
    from pydantic_ai import Agent
    from pydantic_ai import BinaryContent as _BC
    from selmakit.config import ModelConfig, build_model

    model = build_model(ModelConfig(
        model=model_str, base_url=base_url or "http://localhost:11434/v1",
    ))
    prompt = question or _DEFAULT_REVIEW_PROMPT
    result = Agent(model).run_sync([prompt, _BC(data=png, media_type="image/png")])
    return result.output


def _as_list(value) -> list | None:
    """Coerce a render_map alias value to a list (or None).

    Accepts a real list, a scalar (→ one-item list), or a JSON-array *string*
    like ``'["a.gpkg"]'`` the model sometimes passes — so an aliased ``layer=…``
    reconciles to ``layers`` cleanly.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return value
    s = str(value).strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
    return [value] if s else None


_INSTRUCTIONS = """\
## Map output

When the user wants to see a result, call `render_map` with the layer file path(s)
to produce an interactive HTML map. Pass several layers to stack them (e.g. study
area + result). Both **vector** and **raster** layers work: a raster (`.tif`, a
DEM/slope/index) is shown as an image overlay — single-band rasters are colourised
with `cmap` (e.g. a DEM or TRI), multi-band as an RGB composite. You can stack a
raster under vector layers (e.g. a hillshade with roads on top).

For a **choropleth** (a thematic map coloured by a data value — e.g. population
density per municipality), pass `column` = the field to classify. Tune it with
`scheme` (e.g. "NaturalBreaks", "Quantiles", "EqualInterval"), `k` (class count),
and `cmap` (a matplotlib colormap like "YlOrRd", "Blues"). The choropleth styling
applies to whichever layer contains `column`; other layers keep a plain colour, so
you can still stack a boundary or context layer under it.

`column` is a **single** column name (one field to colour by) — NOT a list of
attributes to display. To show names/addresses in the point/feature popups, pass
`fields=["name", "addr:street", ...]` instead. Don't put several comma-joined
names in `column`.

Then report the **exact `output` path that render_map returned**, verbatim — not
the path you passed in. The dashboard embeds the map inline only when that precise
(absolute) path appears in your reply, so quoting the returned path is what makes
the map actually show up.

If render_map returns **`embedded: false`** (with `recommend_tool: "qgis_show"`),
the layer is **too large for an inline web map** — no HTML was kept. Do NOT quote
any path. Instead tell the user the layer is too big to show inline and **offer to
open it in QGIS Desktop** via `qgis_show` (after they confirm). For large layers,
prefer `qgis_show` from the start rather than attempting a heavy inline map.\
"""

_VISION_INSTRUCTIONS = """

## Visual validation

Before finalising a non-trivial result, call `inspect_map(layers=[...])` to render
a static snapshot and **look at it** — a second check alongside `check_crs` /
`sanity_check_result` that catches what numbers miss. Judge:
- **Placement** — is the data where the place actually is? (off-coast / wrong
  hemisphere ⇒ a CRS or lon/lat-swap bug.)
- **Extent** — does the footprint match the expected area?
- **Coverage** — do partition layers (Voronoi, districts) tile without gaps/overlaps?
- **Choropleth** — does the colour actually vary? (uniform ⇒ a broken join or a
  constant/null field.)
- **Index maps** — does NDWI/NDVI water/vegetation follow real features, not cloud?

If the picture contradicts the task, diagnose and **redo the offending step**
(reproject, re-join, pick the right layer) rather than reporting a wrong result.
Pass `column` for a choropleth snapshot; `question` to focus the check.

**If you cannot actually see the attached image** (you would say "I see no image"),
you are not a vision model — call `inspect_map(..., via_vision_model=True)` and the
configured fallback vision model looks at the snapshot for you and returns a written
verdict you can act on.\
"""


@dataclass
class MapOutputCapability(AbstractCapability[Any]):
    """Render vector layers to an interactive HTML map via folium."""

    workspace: str = DEFAULT_WORKSPACE
    # Fallback vision model + its base_url (from agent_build/config). Used by
    # inspect_map only when the main model reports it cannot see the snapshot.
    vision_model: str = ""
    base_url: str = ""

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS + _VISION_INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace
        vision_model = self.vision_model
        base_url = self.base_url

        def render_map(
            output_path: str,
            layers: list[str] | None = None,
            title: str = "",
            basemap: str = "OpenStreetMap",
            basemap_attribution: str = "",
            column: str | None = None,
            scheme: str = "NaturalBreaks",
            k: int = 5,
            cmap: str = "YlOrRd",
            legend: bool = True,
            fields: list[str] | None = None,
            wms_url: str = "",
            wms_layer: str = "",
            wms_format: str = "image/png",
            wms_attribution: str = "",
            # Tolerant aliases for the plural/singular names the model reaches
            # for (`layer`↔`layers`, `columns`↔`column`, `field`↔`fields`) —
            # accepted so a mis-named arg reconciles instead of crashing.
            layer: str | list[str] | None = None,
            columns: str | list[str] | None = None,
            field: str | list[str] | None = None,
        ) -> dict:
            """Render one or more vector or raster layers to an interactive HTML map.

            ``layers`` is a list of file paths, drawn bottom-to-top in order.
            A raster (``.tif`` DEM/slope/index) is shown as an image overlay —
            single-band colourised with ``cmap``, multi-band as an RGB composite —
            and can be stacked under vector layers.
            ``basemap`` is the background tiles — a folium provider name
            ("OpenStreetMap", "CartoDB positron", "CartoDB dark_matter") or an
            XYZ URL template (then pass ``basemap_attribution``). Writes a
            standalone HTML file to ``output_path`` and returns its absolute path
            (report that path verbatim so the dashboard embeds the map inline).
            Layers are reprojected to WGS84 for web display automatically.

            For a **choropleth**, pass ``column`` (the numeric field to classify);
            the layer holding it is coloured by a classified ramp with a legend.
            ``scheme`` is a classification scheme ("NaturalBreaks", "Quantiles",
            "EqualInterval", …), ``k`` the class count, ``cmap`` a matplotlib
            colormap. Layers without ``column`` keep a plain colour.

            The embedded GeoJSON (geometry + attributes) is ~90% of the HTML, so
            each layer keeps ONLY the columns that are displayed: the geometry,
            the choropleth ``column``, and any attribute names listed in
            ``fields`` (also what tooltips show). Every other column is dropped —
            pass ``fields`` to keep e.g. ["name", "height"] in the popup. This,
            plus canvas rendering, is what lets a very large layer (100k+
            features) stay a viable inline map.

            An OGC **WMS** service can be overlaid live: pass ``wms_url`` + a
            ``wms_layer`` name (from ``wms_capabilities``); the service renders
            its tiles into the map (display only — a WMS is pictures, not
            data). Works on top of ``layers`` or standalone (then the map
            centres on the WMS layer's advertised bbox).
            """
            # Reconcile the tolerant aliases onto the canonical params.
            if not layers:
                layers = _as_list(layer)
            if fields is None and field is not None:
                fields = _as_list(field)
            if column is None and columns is not None:
                col = _as_list(columns) or []
                # The model reaches for `columns` (plural) as a *display-field
                # list* and comma-joins it into one string ("shop,name") — but a
                # choropleth is a single numeric column. Split the string, and if
                # several names result they were meant as popup `fields`, not one
                # choropleth: route them there instead of erroring on a bogus
                # "shop,name" column name.
                if len(col) == 1 and "," in col[0]:
                    col = [c.strip() for c in col[0].split(",") if c.strip()]
                if len(col) > 1:
                    merged = list(fields) if fields else []
                    merged += [c for c in col if c not in merged]
                    fields = merged
                elif col:
                    column = col[0]

            if not layers and not (wms_url and wms_layer):
                return {"ok": False, "error": "no layers given"}
            layers = layers or []
            # Absolute so the dashboard's os.path.isfile() resolves it regardless
            # of which directory the dashboard process runs from.
            output_path = str(Path(resolve_path(output_path, ws)).resolve())
            try:
                import geopandas as gpd

                # Guard (cheap, before the heavy read/render): count features first.
                # A very large layer makes an inline web map that is slow to build
                # and heavy enough to freeze the dashboard — steer it to QGIS instead.
                total_features = 0
                for path in layers:
                    try:
                        import pyogrio

                        total_features += int(
                            pyogrio.read_info(resolve_path(path, ws))["features"]
                        )
                    except Exception:  # noqa: BLE001 — unknown count must not block
                        pass
                if total_features > _MAX_INLINE_FEATURES:
                    return {
                        "ok": True,
                        "embedded": False,
                        "features": total_features,
                        "reason": (
                            f"{total_features} features exceed the inline-map limit "
                            f"({_MAX_INLINE_FEATURES}); an inline web map would be too "
                            "heavy for the dashboard."
                        ),
                        "recommend_tool": "qgis_show",
                    }

                fmap = None
                drawn = []
                drawn_resolved: list[str] = []  # absolute paths, for /qgis
                choro_applied = False
                raster_drawn = False
                attributions: set[str] = set()
                available_columns: set[str] = set()  # union, for a helpful error
                for i, path in enumerate(layers):
                    resolved = resolve_path(path, ws)

                    # Raster layer → a colourised ImageOverlay (WGS84), not the
                    # vector reader (which can't open a GeoTIFF).
                    if _is_raster(path):
                        import folium

                        rgba, bounds = _raster_rgba_and_bounds(resolved, cmap)
                        if fmap is None:  # raster is the base — make the map ourselves
                            center = [
                                (bounds[0][0] + bounds[1][0]) / 2,
                                (bounds[0][1] + bounds[1][1]) / 2,
                            ]
                            fmap = folium.Map(
                                location=center, tiles=basemap,
                                attr=basemap_attribution or None,
                            )
                        folium.raster_layers.ImageOverlay(
                            image=rgba, bounds=bounds, opacity=0.75,
                            name=path.split("/")[-1],
                        ).add_to(fmap)
                        raster_drawn = True
                        drawn.append(path)
                        drawn_resolved.append(str(Path(resolved).resolve()))
                        meta = provenance.read_meta(resolved)
                        if meta and meta.get("licence"):
                            attributions.add(meta["licence"])
                        continue

                    gdf = gpd.read_file(resolved)
                    if gdf.empty:
                        continue
                    available_columns.update(
                        c for c in gdf.columns if c != gdf.geometry.name
                    )
                    if gdf.crs and gdf.crs.to_epsg() != 4326:
                        gdf = gdf.to_crs(4326)
                    # Keep only the columns that will be drawn/shown: geometry,
                    # the choropleth column, and any requested `fields`. The
                    # embedded GeoJSON dominates the HTML size, so dropping unused
                    # attribute columns is the single biggest size win.
                    keep = set(fields or [])
                    if column and column in gdf.columns:
                        keep.add(column)
                    gdf = gdf[[c for c in gdf.columns
                               if c == gdf.geometry.name or c in keep]]
                    explore_kwargs: dict = {
                        "m": fmap,
                        "name": path.split("/")[-1],
                    }
                    # Choropleth for the layer that carries the requested column;
                    # other layers fall back to a plain stacking colour.
                    if column and column in gdf.columns:
                        # Clamp class count to the distinct values present — a
                        # scheme like NaturalBreaks fails when k exceeds them.
                        use_k = max(1, min(k, int(gdf[column].nunique(dropna=True))))
                        explore_kwargs.update(
                            column=column, scheme=scheme, k=use_k, cmap=cmap,
                            legend=legend, style_kwds={"fillOpacity": 0.7},
                        )
                        choro_applied = True
                        choro_k = use_k
                    else:
                        explore_kwargs["color"] = _COLORS[i % len(_COLORS)]
                        explore_kwargs["style_kwds"] = {"fillOpacity": 0.3}
                    if fmap is None:  # the first layer sets the base tiles
                        explore_kwargs["tiles"] = basemap
                        # Canvas rendering draws all features onto one <canvas>
                        # instead of one DOM/SVG node each — the difference
                        # between "loads" and "usable" at 100k+ features.
                        explore_kwargs["prefer_canvas"] = True
                        if basemap_attribution:
                            explore_kwargs["attr"] = basemap_attribution
                    fmap = gdf.explore(**explore_kwargs)
                    drawn.append(path)
                    drawn_resolved.append(str(Path(resolved).resolve()))
                    # Attribution from the layer's provenance sidecar (OSM/basemaps
                    # are licensed and must be credited on the rendered map).
                    meta = provenance.read_meta(resolved)
                    if meta and meta.get("licence"):
                        attributions.add(meta["licence"])

                # WMS-only map: no local layers set the extent, so centre on the
                # WMS layer's advertised WGS84 bbox (one capabilities request).
                if fmap is None and wms_url and wms_layer:
                    import folium

                    center, zoom = [51.0, 10.0], 6  # fallback: DE overview
                    try:
                        from owslib.wms import WebMapService

                        for ver in ("1.3.0", "1.1.1"):
                            try:
                                wms = WebMapService(url=wms_url, version=ver)
                                bb = getattr(wms.contents.get(wms_layer),
                                             "boundingBoxWGS84", None)
                                if bb:
                                    center = [(bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2]
                                    zoom = 10
                                break
                            except Exception:  # noqa: BLE001 - try older version
                                continue
                    except Exception:  # noqa: BLE001 - capabilities are best-effort
                        pass
                    fmap = folium.Map(location=center, zoom_start=zoom,
                                      tiles=basemap,
                                      attr=basemap_attribution or None)

                if fmap is None:
                    return {"ok": False, "error": "all given layers were empty"}

                wms_added = False
                if wms_url and wms_layer:
                    import folium

                    folium.WmsTileLayer(
                        url=wms_url, layers=wms_layer, fmt=wms_format,
                        transparent=True, overlay=True, name=f"WMS: {wms_layer}",
                        attr=wms_attribution or wms_url,
                    ).add_to(fmap)
                    wms_added = True
                    if wms_attribution:
                        attributions.add(wms_attribution)
                # A column that matched no vector layer is only an error when no
                # raster was drawn — `column` simply doesn't apply to a raster.
                if column and not choro_applied and not raster_drawn:
                    cols = ", ".join(sorted(available_columns)) or "(none)"
                    return {
                        "ok": False,
                        "error": (
                            f"column '{column}' not found in any layer. "
                            f"`column` is a SINGLE column to colour a choropleth by "
                            f"(usually numeric); it is not a list of popup fields — "
                            f"pass those as `fields=[...]`. Available columns: {cols}"
                        ),
                    }

                attribution = " · ".join(sorted(attributions))
                try:
                    import folium

                    folium.LayerControl().add_to(fmap)
                    if title:
                        folium.map.Marker(
                            [0, 0],
                            icon=folium.DivIcon(
                                html=f'<div style="font-weight:bold">{title}</div>'
                            ),
                        )
                    if attribution:
                        fmap.get_root().html.add_child(
                            folium.Element(
                                '<div style="position:fixed;bottom:8px;left:8px;'
                                "z-index:9999;background:rgba(255,255,255,0.8);"
                                "padding:2px 6px;font-size:11px;border-radius:3px;"
                                'font-family:sans-serif">'
                                f"{attribution}</div>"
                            )
                        )
                except Exception:  # noqa: BLE001 - layer control/caption are cosmetic
                    pass

                fmap.save(output_path)
                # Hard backstop: if the produced HTML is too big to embed safely,
                # delete it and steer to QGIS rather than freeze the dashboard.
                size_mb = Path(output_path).stat().st_size / (1024 * 1024)
                if size_mb > _MAX_INLINE_MB:
                    Path(output_path).unlink(missing_ok=True)
                    return {
                        "ok": True,
                        "embedded": False,
                        "size_mb": round(size_mb, 1),
                        "reason": (
                            f"the rendered map is {size_mb:.0f} MB (limit "
                            f"{_MAX_INLINE_MB} MB) — too heavy to embed in the chat."
                        ),
                        "recommend_tool": "qgis_show",
                    }
                # Pointer to the last rendered map so the `/qgis` command can open
                # the SAME source layers in QGIS Desktop (QGIS can't read the
                # Folium HTML). Best-effort — a failed pointer never breaks the map.
                try:
                    import json as _json

                    (Path(output_path).parent / "last_map.json").write_text(
                        _json.dumps({"html": output_path,
                                     "layers": drawn_resolved,
                                     "column": column}, indent=2),
                        encoding="utf-8",
                    )
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            result = {"ok": True, "output": output_path, "layers": drawn}
            if wms_added:
                result["wms"] = {"url": wms_url, "layer": wms_layer}
            if choro_applied:
                result["choropleth"] = {"column": column, "scheme": scheme,
                                        "k": choro_k, "cmap": cmap}
            if attribution:
                result["attribution"] = sorted(attributions)
            return result

        def inspect_map(
            layers: list[str] | None = None,
            question: str | None = None,
            column: str | None = None,
            cmap: str = "YlOrRd",
            scheme: str = "NaturalBreaks",
            k: int = 5,
            via_vision_model: bool = False,
            # Tolerant aliases for the names the model reaches for from
            # `render_map` (`layer`↔`layers`, `lines`→another layer,
            # `columns`↔`column`) — accepted and reconciled so a mis-named
            # arg does not crash the run. `field`/`fields` are display-field
            # lists render_map takes; the flat snapshot has no popups, so they
            # are accepted and ignored rather than raising a validation error.
            layer: str | list[str] | None = None,
            lines: str | list[str] | None = None,
            columns: str | list[str] | None = None,
            field: str | list[str] | None = None,
            fields: str | list[str] | None = None,
        ) -> Any:
            """Render a static snapshot of ``layers`` and return it to *look at*.

            A visual correctness check to run before finalising a non-trivial
            result — a second channel alongside `check_crs`/`sanity_check_result`.
            It draws the layers (raster and/or vector, choropleth if ``column``
            fits) as one flat image and, by default, returns that image plus a
            per-layer fact summary (features, geometry, CRS, WGS84 extent, value
            range) so you can judge placement, extent, coverage, and colour
            variation, then redo the offending step if the picture contradicts the
            task. ``question`` focuses the check.

            **If you cannot see the returned image** (you are a text-only model),
            call again with ``via_vision_model=True``: the configured fallback
            vision model (``model.vision_model``) looks at the snapshot and returns
            a written verdict instead of the image.
            """
            # Reconcile the tolerant aliases onto the canonical params, mirroring
            # render_map, so an arg named like render_map's reconciles instead of
            # crashing the run (a repeated mis-call raises UnexpectedModelBehavior).
            layers = _as_list(layers) or []
            layers += [p for p in (_as_list(layer) or []) if p not in layers]
            layers += [p for p in (_as_list(lines) or []) if p not in layers]
            if column is None and columns is not None:
                col = _as_list(columns) or []
                if len(col) == 1 and "," in col[0]:
                    col = [c.strip() for c in col[0].split(",") if c.strip()]
                if col:
                    column = col[0]
            _ = (field, fields)  # display-field lists — no popups here; ignored.

            if not layers:
                return {"ok": False, "error": "no layers given"}
            try:
                png, summary = _render_snapshot(
                    layers, ws, column=column, scheme=scheme, k=k, cmap=cmap,
                    title=question or "result — visual check",
                )
                # Keep the snapshot as a cache artefact (best-effort).
                snap = str(Path(resolve_path("inspect_snapshot.png", ws)))
                try:
                    Path(snap).write_bytes(png)
                except OSError:
                    snap = None
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            if via_vision_model:
                # Main model can't see → the configured vision model looks instead.
                if not vision_model:
                    return {
                        "ok": False, "vision_model": None, "layers": summary,
                        "note": "no fallback vision model configured — set "
                        "model.vision_model in .chester/chester.json "
                        "(e.g. 'ollama/llava:latest').",
                    }
                try:
                    review = _ask_vision_model(vision_model, base_url, png, question)
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "layers": summary,
                            "error": f"vision model '{vision_model}' failed: "
                            f"{type(exc).__name__}: {exc}"}
                return {"ok": True, "reviewed_by": vision_model, "review": review,
                        "layers": summary, "snapshot": snap}

            return ToolReturn(
                return_value={
                    "ok": True, "layers": summary, "snapshot": snap,
                    "instructions": "Look at the attached snapshot: is the data "
                    "placed correctly, the extent plausible, coverage complete, "
                    "the colour varying? If it contradicts the task, redo the "
                    "offending step. If you CANNOT see the image, call inspect_map "
                    "again with via_vision_model=true.",
                },
                content=[
                    "Rendered snapshot for visual validation:",
                    BinaryContent(data=png, media_type="image/png"),
                ],
            )

        return FunctionToolset(tools=[render_map, inspect_map])
