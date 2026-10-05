"""The three guards of the map output — and the picture as the way out. A pure core.

Phase KM, step 1.5, second cut. The seam is one of content: `maprender.py` **builds**
the map; this says when it is **not delivered**. Each of the three guards came from a
run where `ok: true` came back and the reader saw nothing — a frozen dashboard, a white
page, 490 MB of HTML. They are why this file is more than a collection of constants.

The order is deliberate, cheap to expensive: feature count **before** reading, vertices
after reading, file size after writing.

**The refusals are written for a model, not for a log.** They say explicitly whether a
file was produced — an answer linking to a map that does not exist is worse than a
refusal.
"""

from __future__ import annotations

from pathlib import Path

from chester.maprender import MapStyle
from chester.mapsnapshot import _render_snapshot
from chester.workspace import resolve_path

#: Cheap pre-check, before anything is read or drawn.
MAX_INLINE_FEATURES = 50_000

#: Hard backstop on the produced HTML size.
MAX_INLINE_MB = 45

# The guard that actually predicts a white page. Feature count and byte size both
# missed the case that motivated this: 6.888 contour lines — far under the 50.000
# feature limit — inlined as 38,5 MB of coordinates on a **single 40 MB line**, and
# the finished HTML came to 42,5 MB, just under the 45 MB backstop. Both guards said
# "fine"; the browser showed a white screen (2026-09-02,
# `pluvial-flow-accumulation-tegernheim`). What costs the browser is neither the
# number of features nor the bytes on disk but the number of vertices it must parse
# and turn into paths, so that is what is counted. The observed case measures
# 936.687 vertices; an ordinary city layer sits at a few tens of thousands. 500k
# separates the two by a wide margin in both directions.
MAX_INLINE_VERTICES = 500_000


def picture_beside(html_path: str, layers: list[str], ws: str,
                   style: MapStyle, title: str) -> str | None:
    """A flat picture of the same map, written beside the HTML. ``None`` on failure.

    The interactive map and the picture are two forms of one artefact, not two
    answers. The picture is useful on its own: a vision model cannot read HTML (the
    visual check already renders one for exactly that reason), a report can embed
    it, and it survives without a browser, a network or the CDN libraries the HTML
    builds itself from.

    Written unconditionally and never mentioned to the model. **Which form a reader
    receives is not decided here** — a channel that can show HTML shows it, one that
    cannot looks beside it. Making this conditional on the destination would put
    channel knowledge into a tool, and the same agent serves the web chat, a chat
    bot and the CLI.

    Never fatal: the interactive map is the primary artefact and a failed picture
    must not cost the caller their result.
    """
    try:
        png_bytes, _summary = _render_snapshot(
            layers, ws, style.column, style.scheme, style.k, style.cmap, title
        )
        target = str(Path(html_path).with_suffix(".png"))
        Path(target).write_bytes(png_bytes)
        return target
    except Exception:  # noqa: BLE001 - a second form is never worth a failed map
        return None


def refuse_if_too_many_features(layers: list[str], workspace: str) -> dict | None:
    """Refuse before anything is read — or ``None`` when the layers fit.

    Past :data:`MAX_INLINE_FEATURES` an embedded web map is heavy enough to freeze
    the dashboard; steer the caller to QGIS instead.

    An unreadable count never blocks — whatever fails here is caught by the two
    guards downstream.
    """
    total_features = 0
    for path in layers:
        try:
            import pyogrio

            total_features += int(
                pyogrio.read_info(resolve_path(path, workspace))["features"]
            )
        except Exception:  # noqa: BLE001 — unknown count must not block
            pass
    if total_features > MAX_INLINE_FEATURES:
        return {
            "ok": False,  # nothing rendered — see the size guard below
            "embedded": False,
            "features": total_features,
            "reason": (
                f"{total_features} features exceed the inline-map limit "
                f"({MAX_INLINE_FEATURES}); an inline web map would be too "
                "heavy for the dashboard. NO file was written; there is no "
                "map to link to."
            ),
            "recommend_tool": "qgis_show",
        }
    return None


def picture_instead_of_map(vertices: int, output_path: str, drawn: list[str],
                           ws: str, style: MapStyle, title: str) -> dict | None:
    """Too many vertices → hand over the picture, not nothing. ``None`` when it fits.

    This guard's predecessor threw the HTML away and reported ``ok: false`` —
    right, as long as nothing usable had been produced. Here it is different: the
    same map exists as a PNG, and a PNG is exactly what a reader with too many
    lines needs anyway. Reporting it as a failure would mean withholding a result
    that is lying on the disk — the mirror image of the same mistake.
    """
    if vertices <= MAX_INLINE_VERTICES:
        return None
    picture = picture_beside(output_path, drawn, ws, style, title)
    Path(output_path).unlink(missing_ok=True)
    if not picture:
        return {
            "ok": False,
            "embedded": False,
            "vertices": vertices,
            "reason": (
                f"{vertices:,} vertices exceed the inline-map "
                f"limit ({MAX_INLINE_VERTICES:,}) and the fallback "
                "picture could not be rendered either. NO file was "
                "written; there is no map to link to."
            ),
            "recommend_tool": "qgis_show",
        }
    return {
        "ok": True,
        "embedded": False,
        "output": picture,
        "picture": picture,
        "layers": drawn,
        "vertices": vertices,
        "reason": (
            f"{vertices:,} vertices exceed the inline-map limit "
            f"({MAX_INLINE_VERTICES:,}) — an interactive web map with "
            "that many would open as a blank page. A STATIC PICTURE was "
            "written instead; report its path, it is a real result. For "
            "an interactive map, reduce the geometry first (a coarser "
            "contour interval, a simplify step, fewer layers) or open the "
            "source layers in QGIS with qgis_show."
        ),
        "recommend_tool": "qgis_show",
    }


def refuse_if_too_large(output_path: str) -> dict | None:
    """The written HTML is too heavy to embed → delete it and say so.

    ``ok: False``: the HTML was written and then deleted, so nothing exists at
    ``output_path``. ``ok: True`` for a file that is not there invites the answer to
    describe a map nobody can open — one run did exactly that, complete with an
    excuse for why it might not appear. No path back means no success to report.
    """
    size_mb = Path(output_path).stat().st_size / (1024 * 1024)
    if size_mb <= MAX_INLINE_MB:
        return None
    Path(output_path).unlink(missing_ok=True)
    return {
        "ok": False,
        "embedded": False,
        "size_mb": round(size_mb, 1),
        "reason": (
            f"the rendered map is {size_mb:.0f} MB (limit "
            f"{MAX_INLINE_MB} MB) — too heavy to embed in the chat. "
            "NO file was written; there is no map to link to."
        ),
        "recommend_tool": "qgis_show",
    }
