"""Make a raster viewable — read decimated, stretched, without an intermediate file.

Why this is here and not in the bench: the house rule "you check an artifact by opening
it" applies to every surface that shows runs. Until 2026-09-01 a GeoTIFF was one line of
text in the bench — of all things in the dialogue whose whole subject is a GeoTIFF
(`map-then-geotiff`). Whoever cannot see that an image is black relies on `ok: true`
again.

Pure: numpy and rasterio, no SelmaKit, no capability. Returns numbers, not sentences —
what the numbers are called is up to the surface showing them.
"""

from __future__ import annotations

from typing import Any

import numpy as np

#: Edge length large rasters are read at. A set of DOP tiles easily has 20,000 px;
#: loading full resolution to squeeze it into a 700 px field costs seconds and memory
#: for nothing. rasterio decimates while reading.
MAX_PX = 900


def _stretch(band: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Stretch one band to 0…255 — constant bands stay what they are."""
    if not valid.any():
        return np.zeros(band.shape, dtype="uint8")
    lo, hi = float(band[valid].min()), float(band[valid].max())
    if hi <= lo:
        # A band with **one** value. Deliberately not brightened when that value is
        # 0: a raster of nothing but zeros is meant to appear as a black area —
        # exactly that picture was the incident of 2026-08-27, and normalising it
        # away would hide the finding.
        #
        # The mask must apply here too. Without it the GeoTIFF of 2026-09-01
        # (`found_buildings.tif`, burn value 1 on nodata 0) became an even white
        # area: the four burnt buildings and the empty rest looked identical, so
        # the preview was as blind as the line of text it was meant to replace.
        return np.where(valid, 0 if lo == 0 else 255, 0).astype("uint8")
    out = (band.astype("float64") - lo) / (hi - lo) * 255.0
    return np.clip(np.where(valid, out, 0), 0, 255).astype("uint8")


def preview(path: str, max_px: int = MAX_PX) -> tuple[np.ndarray, dict[str, Any]] | None:
    """An image (H×W or H×W×3, uint8) and its facts — ``None`` when unreadable.

    The facts are those that reveal a broken raster without guessing: ``bands``,
    ``size``, ``crs``, ``range`` (min/max of the valid pixels) and ``all_nodata``.
    ``range`` with min == max is the black area.
    """
    try:
        import rasterio
        from rasterio.enums import Resampling
    except ImportError:  # pragma: no cover - rasterio is part of the core
        return None
    try:
        with rasterio.open(path) as src:
            scale = max(src.width, src.height) / float(max_px)
            shape = ((int(src.height / scale), int(src.width / scale))
                     if scale > 1 else (src.height, src.width))
            count = min(src.count, 3)
            data = src.read(list(range(1, count + 1)), out_shape=(count, *shape),
                            resampling=Resampling.nearest, masked=True)
            facts: dict[str, Any] = {
                "bands": src.count,
                "size": [src.width, src.height],
                "crs": str(src.crs) if src.crs else None,
            }
    except Exception:  # noqa: BLE001 - a preview must never make the UI raise
        return None

    valid = ~np.ma.getmaskarray(data)
    filled = np.ma.getdata(data).astype("float64")
    facts["all_nodata"] = not bool(valid.any())
    facts["range"] = ([float(filled[valid].min()), float(filled[valid].max())]
                      if valid.any() else None)
    bands = [_stretch(filled[i], valid[i]) for i in range(data.shape[0])]
    # Two bands are not RGB — the first alone then shows more than a jumbled colour.
    image = np.dstack(bands) if len(bands) == 3 else bands[0]
    return image, facts


def write_png(path: str, out_path: str, max_px: int = MAX_PX) -> dict[str, Any] | None:
    """Put the same image as a PNG beside the GeoTIFF — ``None`` when that failed.

    A GeoTIFF is a data format, not an image: no chat channel shows it, no vision model
    reads it, and in the browser it stays a download. The same rule by which
    `render_map` puts a flat picture beside the HTML map (`mapoutput`,
    `_write_picture_beside`) — one artifact in two forms, not two results.

    The georeferenced numbers stay in the TIFF; the PNG is there for **looking**.
    """
    made = preview(path, max_px)
    if made is None:
        return None
    image, facts = made
    try:
        from PIL import Image

        Image.fromarray(image).save(out_path)
    except Exception:  # noqa: BLE001 - a picture on the side must never cost the result
        return None
    return facts
