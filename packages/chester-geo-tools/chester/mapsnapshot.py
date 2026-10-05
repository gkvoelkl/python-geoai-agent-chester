"""The **still image** of a map — a pure core, not a tool.

This is what turns layers into a PNG: convert rasters to RGBA, lay an aerial basemap
underneath, pad the extent, put a colour scale beside it and draw the whole. Two tools
use it — `render_map` puts the still beside the HTML map, `inspect_map` sends it to a
vision model — and neither belongs here.

Split out on 2026-09-14 (Phase KM, step 1): `capabilities/mapoutput.py` carried 1295
lines, 634 of them module level. The cut makes the drawing machinery testable without
a framework and shares it honestly between both tools, instead of leaving it with the
map tool where the visual check borrows it.

**Two traps are encoded here** and must not vanish when touched: `_is_blank_image` —
an empty tile as background looked like a map for months; and `_pad_extent` — an
extent without padding cuts exactly the objects that are to be judged.
"""

from __future__ import annotations

from chester.workspace import resolve_path

_COLORS = ["#3388ff", "#e6550d", "#31a354", "#756bb1", "#d62728", "#17becf"]


def _is_raster(path: str) -> bool:
    import os

    return os.path.splitext(path)[1].lower() in _RASTER_EXTS


def _raster_rgba_and_bounds(resolved: str, cmap: str):
    """Read a raster (decimated if large), reproject to WGS84, and return
    ``(rgba_uint8, [[south, west], [north, east]], scale)`` ready for an
    ImageOverlay. ``scale`` carries the colour ramp's vmin/vmax (``None`` for an
    RGB composite) so a caller can label it."""
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
        src_transform = src.transform * src.transform.scale(src.width / out_w, src.height / out_h)
        if src.crs and src.crs.to_epsg() != 4326:
            bounds = array_bounds(out_h, out_w, src_transform)
            transform, width, height = calculate_default_transform(
                src.crs, "EPSG:4326", out_w, out_h, *bounds
            )
            dst = np.full((bands, height, width), np.nan, dtype="float32")
            for b in range(bands):
                reproject(
                    source=data[b],
                    destination=dst[b],
                    src_transform=src_transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs="EPSG:4326",
                    src_nodata=nodata,
                    dst_nodata=np.nan,
                    resampling=Resampling.nearest,
                )
            data, nodata = dst, np.nan
            west, south, east, north = array_bounds(height, width, transform)
        else:
            west, south, east, north = array_bounds(out_h, out_w, src_transform)

    rgba, scale = _raster_to_rgba(data, nodata, cmap)
    return rgba, [[south, west], [north, east]], scale


_RASTER_EXTS = {
    ".tif",
    ".tiff",
    ".vrt",
    ".img",
    ".asc",
    ".jp2",
    ".hgt",
    ".dem",
    ".dt2",
    ".bil",
}


_MAX_RASTER_PX = 2000


def _raster_to_rgba(data, nodata, cmap: str):
    """Turn a (bands, H, W) float array into an (H, W, 4) uint8 RGBA image.

    Single-band → colourised with ``cmap`` over a 2–98 percentile stretch;
    3+ bands → an RGB composite (per-band stretch). NoData / non-finite pixels
    are made transparent.

    Returns ``(rgba, scale)``. ``scale`` is ``{"vmin": …, "vmax": …}`` for the
    colourised single-band case and ``None`` for an RGB composite — the caller
    needs it to draw a colour bar, and without one nobody can tell which end of
    the ramp is high. A vision model asked to read an unlabelled `YlOrRd` NDVI
    called the pale river "vegetation" and the dark parks "non-vegetated": pale
    is the *low* end, and it had to guess (`doc/visual-validation.md` §7).
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
                scaled = np.clip((band - lo) / (hi - lo + 1e-9) * 255, 0, 255)
                # Non-finite pixels must be zeroed *before* the uint8 cast: the
                # reprojection fills uncovered corners with NaN, and casting NaN to
                # an integer is undefined (it warns and yields garbage). The alpha
                # channel below hides those pixels, but only if they are valid bytes.
                out[..., c] = np.nan_to_num(scaled, nan=0.0, posinf=255.0, neginf=0.0)
            opaque &= finite
        out[..., 3] = np.where(opaque, 255, 0)
        return out, None  # an RGB composite has no single scale to label

    band = data[0]
    mask = ~np.isfinite(band)
    if nodata is not None:
        mask |= band == nodata
    vals = band[~mask]
    vmin, vmax = np.percentile(vals, [2, 98]) if vals.size else (0.0, 1.0)
    norm = (np.clip(band, vmin, vmax) - vmin) / (vmax - vmin + 1e-9)
    rgba = (matplotlib.colormaps[cmap](norm) * 255).astype("uint8")
    rgba[mask, 3] = 0
    return rgba, {"vmin": float(vmin), "vmax": float(vmax)}


_TILE_USER_AGENT = "chester-geo-ai/0.1 (+https://github.com/gkvoelkl/python-geoai-agent-chester)"


_BLANK_IMAGE_STD = 2.0


def _is_blank_image(png: bytes) -> bool:
    """Is this PNG uniform — i.e. a "no coverage" answer rather than a picture?

    A WMS outside its coverage does not fail: it returns HTTP 200 with a blank
    tile. Treating those bytes as a backdrop is worse than having none, because it
    *suppresses the OSM fallback* — which is exactly the case a CRS bug produces
    (data moved into the sea, where no aerial coverage exists). The visual check
    then sees shapes on white and cannot judge placement at all, the one error class
    it exists for. Unreadable bytes count as blank: a picture we cannot inspect is
    not one we should trust.
    """
    import io

    try:
        from PIL import Image, ImageStat

        stat = ImageStat.Stat(Image.open(io.BytesIO(png)).convert("L"))
        return stat.stddev[0] < _BLANK_IMAGE_STD
    except Exception:  # noqa: BLE001 - undecodable is as useless as blank
        return True


def _draw_aerial_backdrop(ax) -> bool:
    """Draw open aerial imagery behind the plot; True if it worked.

    Best-effort throughout — no coverage, no network or a slow service simply
    returns False and the caller falls back to OSM.
    """
    import io

    try:
        from PIL import Image

        from chester import dop

        west, east = ax.get_xlim()
        south, north = ax.get_ylim()
        png = dop.aerial_backdrop_png([west, south, east, north], 900, 700)
        if not png or _is_blank_image(png):
            return False
        ax.imshow(
            Image.open(io.BytesIO(png)),
            extent=[west, east, south, north],
            origin="upper",
            zorder=-1,
        )
        ax.set_xlim(west, east)
        ax.set_ylim(south, north)
        return True
    except Exception:  # noqa: BLE001 - a backdrop is a nicety, not required
        return False


_MIN_SPAN_DEG = 0.02


_PAD_FRACTION = 0.15


def _pad_extent(ax) -> None:
    """Give the frame a real, non-degenerate extent before the backdrop is fetched.

    Three things at once: a margin around the data (a result touching the frame edge
    reads as clipped), a floor under each span (so a single feature still gets a
    map), and a cap on the ratio between the two (a long thin layer otherwise renders
    as a strip in which nothing is recognisable). Called before the basemap, because
    both providers take their bbox from the axis limits.
    """
    west, east = ax.get_xlim()
    south, north = ax.get_ylim()
    mid_x, mid_y = (west + east) / 2, (south + north) / 2
    span_x = max(east - west, _MIN_SPAN_DEG)
    span_y = max(north - south, _MIN_SPAN_DEG)
    span_x, span_y = max(span_x, span_y / 2), max(span_y, span_x / 2)
    span_x *= 1 + _PAD_FRACTION
    span_y *= 1 + _PAD_FRACTION
    ax.set_xlim(mid_x - span_x / 2, mid_x + span_x / 2)
    ax.set_ylim(mid_y - span_y / 2, mid_y + span_y / 2)


def _add_colourbar(fig, ax, cmap: str, scale: dict, label: str) -> None:
    """Label a colourised raster's ramp with its actual values.

    Without this the picture states no units and no direction, so reading it is
    guesswork — and a guess arrives worded exactly like knowledge. Small and
    horizontal so it cannot crowd out the map it explains.
    """
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize

    sm = ScalarMappable(norm=Normalize(vmin=scale["vmin"], vmax=scale["vmax"]), cmap=cmap)
    bar = fig.colorbar(sm, ax=ax, orientation="horizontal", fraction=0.04, pad=0.06)
    bar.set_label(label, fontsize=8)
    bar.ax.tick_params(labelsize=7)


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
            rgba, ((south, west), (north, east)), scale = _raster_rgba_and_bounds(resolved, cmap)
            ax.imshow(rgba, extent=[west, east, south, north], origin="upper", zorder=i)
            info = {
                "layer": name,
                "type": "raster",
                "extent_wgs84": [
                    round(west, 5),
                    round(south, 5),
                    round(east, 5),
                    round(north, 5),
                ],
            }
            if scale:
                _add_colourbar(fig, ax, cmap, scale, name)
                info["value_range"] = [round(scale["vmin"], 4), round(scale["vmax"], 4)]
            summary.append(info)
            continue

        gdf = gpd.read_file(resolved)
        if gdf.empty:
            summary.append({"layer": name, "type": "vector", "features": 0})
            continue
        crs = gdf.crs.to_string() if gdf.crs else None
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(4326)
        info = {
            "layer": name,
            "type": "vector",
            "features": len(gdf),
            "geometry_types": sorted({g.geom_type for g in gdf.geometry if g is not None}),
            "crs": crs,
            "extent_wgs84": [round(float(v), 5) for v in gdf.total_bounds],
        }
        if column and column in gdf.columns:
            use_k = max(1, min(k, int(gdf[column].nunique(dropna=True))))
            gdf.plot(
                ax=ax,
                column=column,
                scheme=scheme,
                k=use_k,
                cmap=cmap,
                legend=True,
                edgecolor="grey",
                linewidth=0.3,
                zorder=i,
            )
            vals = gdf[column].dropna()
            info["column"] = column
            info["colour"] = f"shaded by {column} with the {cmap} colourmap"
            info["value_range"] = [float(vals.min()), float(vals.max())] if len(vals) else None
        else:
            # Lines get drawn heavier and near-opaque: at 0.3/0.6 a network of 171
            # cycleways vanished into the aerial texture, and the vision model was
            # then judging the backdrop rather than the result. Polygons keep the
            # thin outline — widening it turns a few thousand of them into a blot.
            line_only = info["geometry_types"] and all("Line" in t for t in info["geometry_types"])
            info["colour"] = _COLORS[i % len(_COLORS)]
            gdf.plot(
                ax=ax,
                color=_COLORS[i % len(_COLORS)],
                edgecolor="black",
                linewidth=1.6 if line_only else 0.3,
                alpha=0.9 if line_only else 0.6,
                zorder=i,
            )
        summary.append(info)

    # A basemap under the data makes geographic placement legible — without it a
    # vector snapshot is shapes on white, and an off-coast / wrong-CRS layer looks
    # fine. Best-effort: needs contextily + a network tile fetch, so any failure
    # (offline, import missing) just leaves the plain plot.
    if summary and any(s.get("type") == "vector" for s in summary):
        # Before either provider: both read their bbox off the axis limits, so a
        # degenerate extent has to be widened here or the backdrop covers a sliver.
        _pad_extent(ax)
        # Prefer *aerial* imagery where it exists: the vision model is asked whether
        # the result sits where it should, and a photo shows the actual buildings and
        # field edges an OSM rendering only symbolises. One WMS GetMap (~70 KB), not
        # the 18-91 MB data tiles `fetch_dop` pulls — a backdrop wants a picture.
        # OSM stays the fallback: outside the covered states, and its labels are the
        # better cue for "is this the right *place*".
        if not _draw_aerial_backdrop(ax):
            try:
                import contextily as cx

                cx.add_basemap(
                    ax,
                    crs="EPSG:4326",
                    source=cx.providers.OpenStreetMap.Mapnik,
                    headers={"User-Agent": _TILE_USER_AGENT},
                    # Explicitly behind the data. contextily leaves a base source at
                    # matplotlib's default image zorder (0), which *ties* with the
                    # first vector layer's `zorder=i` — and a tie is broken by draw
                    # order, so the basemap landed on top and hid the result. The
                    # aerial branch above sets zorder=-1 for the same reason.
                    zorder=-1,
                )
            except Exception:  # noqa: BLE001 - the basemap is a nicety, not required
                pass

    ax.set_title(title)
    ax.set_axis_off()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue(), summary
