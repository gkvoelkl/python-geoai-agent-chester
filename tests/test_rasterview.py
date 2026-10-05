"""Making a raster viewable — the preview and the PNG beside it (`chester/rasterview.py`).

The same shortcoming twice: the incident of 2026-08-27 ended with a 266 MB GeoTIFF nobody
could look at, and on 2026-09-01 the bench showed the same result as a text line
"4 Datei(en)". A GeoTIFF is a data format, not an image: no chat channel displays it, no
vision model reads it. So what is checked here is not the beauty of the preview but that
it **does not hide the findings**.
"""

from __future__ import annotations

import numpy as np
import pytest

from chester.rasterview import preview, write_png


def _raster(path, values, *, nodata=0.0):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    band = np.array(values, dtype="float32")
    with rasterio.open(path, "w", driver="GTiff", height=band.shape[0],
                       width=band.shape[1], count=1, dtype="float32",
                       crs="EPSG:25832", nodata=nodata,
                       transform=from_origin(0, band.shape[0], 1, 1)) as dst:
        dst.write(band, 1)
    return path


def test_a_black_raster_stays_black(tmp_path):
    """A raster of nothing but zeros should appear as a black area.

    Normalising it away would hide the finding — and exactly this finding was the
    incident: an image that was reported as a map.
    """
    made = preview(str(_raster(tmp_path / "black.tif", [[0, 0], [0, 0]], nodata=-1)))
    assert made is not None
    image, facts = made
    assert facts["range"] == [0.0, 0.0]
    assert not image.any(), "eine schwarze Fläche muss schwarz bleiben"


def test_the_nodata_mask_holds_for_a_single_valued_band(tmp_path):
    """Burn value 1 on nodata 0: the burned pixels white, the rest black.

    The first attempt ignored the mask here and turned `found_buildings.tif` (2026-09-01)
    into an even white area — the four buildings and the empty rest looked identical, so
    the preview was just as blind as the text line it was meant to replace.
    """
    made = preview(str(_raster(tmp_path / "burn.tif", [[0, 1], [1, 0]])))
    assert made is not None
    image, facts = made
    assert facts["range"] == [1.0, 1.0]
    assert int((image > 0).sum()) == 2, "nur die gebrannten Pixel sind hell"


def test_a_range_becomes_a_grey_ramp(tmp_path):
    made = preview(str(_raster(tmp_path / "dem.tif", [[10, 20], [30, 40]], nodata=-1)))
    assert made is not None
    image, facts = made
    assert facts["range"] == [10.0, 40.0]
    assert image.min() == 0 and image.max() == 255


def test_write_png_puts_the_picture_beside_the_data(tmp_path):
    from PIL import Image

    tif = _raster(tmp_path / "burned.tif", [[0, 1], [1, 0]])
    png = tmp_path / "burned.png"
    facts = write_png(str(tif), str(png))
    assert facts and facts["bands"] == 1 and facts["crs"] == "EPSG:25832"
    assert png.is_file() and Image.open(png).size == (2, 2)


def test_a_broken_raster_costs_nothing(tmp_path):
    """An image on the side must never cost the result."""
    broken = tmp_path / "not-a-raster.tif"
    broken.write_text("kein GeoTIFF", encoding="utf-8")
    assert preview(str(broken)) is None
    assert write_png(str(broken), str(tmp_path / "x.png")) is None
