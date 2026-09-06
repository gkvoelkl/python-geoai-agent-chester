"""Ein Raster ansehbar machen — die Vorschau und das PNG daneben (`chester/rasterview.py`).

Zweimal derselbe Mangel: Der Vorfall vom 2026-08-27 endete mit einem 266-MB-GeoTIFF,
das niemand ansehen konnte, und am 2026-09-01 zeigte die Bench dasselbe Ergebnis als
Textzeile „4 Datei(en)". Ein GeoTIFF ist ein Datenformat, kein Bild: Kein Chat-Kanal
stellt es dar, kein Sehmodell liest es. Was hier geprüft wird, ist deshalb nicht die
Schönheit der Vorschau, sondern dass sie **die Befunde nicht versteckt**.
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
    """Ein Raster aus lauter Nullen soll als schwarze Fläche erscheinen.

    Es wegzunormieren hieße, den Befund zu verstecken — und genau dieser Befund war
    der Vorfall: ein Bild, das als Karte gemeldet wurde.
    """
    made = preview(str(_raster(tmp_path / "black.tif", [[0, 0], [0, 0]], nodata=-1)))
    assert made is not None
    image, facts = made
    assert facts["range"] == [0.0, 0.0]
    assert not image.any(), "eine schwarze Fläche muss schwarz bleiben"


def test_the_nodata_mask_holds_for_a_single_valued_band(tmp_path):
    """Brennwert 1 auf nodata 0: die gebrannten Pixel weiß, der Rest schwarz.

    Der erste Wurf ignorierte hier die Maske und machte aus `found_buildings.tif`
    (2026-09-01) eine gleichmäßig weiße Fläche — die vier Gebäude und der leere Rest
    sahen identisch aus, also war die Vorschau genauso blind wie die Textzeile, die
    sie ersetzen sollte.
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
    """Ein Bild nebenbei darf das Ergebnis nie kosten."""
    broken = tmp_path / "not-a-raster.tif"
    broken.write_text("kein GeoTIFF", encoding="utf-8")
    assert preview(str(broken)) is None
    assert write_png(str(broken), str(tmp_path / "x.png")) is None
