"""Ein Raster ansehbar machen — decimiert gelesen, gestreckt, ohne Zwischendatei.

Warum das hier steht und nicht in der Bench: Die Hausregel „ein Artefakt prüft man,
indem man es öffnet" gilt für jede Oberfläche, die Läufe zeigt. Ein GeoTIFF war bis
2026-09-01 in der Bench eine Zeile Text — ausgerechnet bei dem Dialog, dessen ganzer
Gegenstand ein GeoTIFF ist (`map-then-geotiff`). Wer nicht sieht, dass ein Bild schwarz
ist, verlässt sich wieder auf `ok: true`.

Rein: numpy und rasterio, kein SelmaKit, keine Capability. Gibt Zahlen zurück, keine
Sätze — wie die Zahlen heißen, entscheidet die Oberfläche, die sie zeigt.
"""

from __future__ import annotations

from typing import Any

import numpy as np

#: Kantenlänge, auf die groß gelesen wird. Ein DOP-Kachelsatz hat schnell 20 000 px;
#: die volle Auflösung zu laden, um sie in ein 700-px-Feld zu quetschen, kostet
#: Sekunden und Speicher für nichts. rasterio decimiert schon beim Lesen.
MAX_PX = 900


def _stretch(band: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Ein Band auf 0…255 strecken — konstante Bänder bleiben, was sie sind."""
    if not valid.any():
        return np.zeros(band.shape, dtype="uint8")
    lo, hi = float(band[valid].min()), float(band[valid].max())
    if hi <= lo:
        # Ein Band mit **einem** Wert. Absichtlich nicht aufgehellt, wenn dieser
        # Wert 0 ist: Ein Raster aus lauter Nullen soll als schwarze Fläche
        # erscheinen — genau dieses Bild war der Vorfall vom 2026-08-27, und es
        # wegzunormieren hieße, den Befund zu verstecken.
        #
        # Die Maske muss auch hier gelten. Ohne sie wurde das GeoTIFF vom
        # 2026-09-01 (`found_buildings.tif`, Brennwert 1 auf nodata 0) zur
        # gleichmäßig weißen Fläche: Die vier gebrannten Gebäude und der leere
        # Rest sahen identisch aus, also war die Vorschau genauso blind wie die
        # Textzeile, die sie ersetzen sollte.
        return np.where(valid, 0 if lo == 0 else 255, 0).astype("uint8")
    out = (band.astype("float64") - lo) / (hi - lo) * 255.0
    return np.clip(np.where(valid, out, 0), 0, 255).astype("uint8")


def preview(path: str, max_px: int = MAX_PX) -> tuple[np.ndarray, dict[str, Any]] | None:
    """Ein Bild (H×B oder H×B×3, uint8) und die Fakten dazu — ``None``, wenn unlesbar.

    Die Fakten sind die, an denen man ein kaputtes Raster erkennt, ohne zu raten:
    ``bands``, ``size``, ``crs``, ``range`` (min/max der gültigen Pixel) und
    ``all_nodata``. ``range`` mit min == max ist die schwarze Fläche.
    """
    try:
        import rasterio
        from rasterio.enums import Resampling
    except ImportError:  # pragma: no cover - rasterio gehört zum Kern
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
    except Exception:  # noqa: BLE001 - eine Vorschau darf die Oberfläche nie werfen
        return None

    valid = ~np.ma.getmaskarray(data)
    filled = np.ma.getdata(data).astype("float64")
    facts["all_nodata"] = not bool(valid.any())
    facts["range"] = ([float(filled[valid].min()), float(filled[valid].max())]
                      if valid.any() else None)
    bands = [_stretch(filled[i], valid[i]) for i in range(data.shape[0])]
    # Zwei Bänder sind kein RGB — dann zeigt das erste allein mehr als eine
    # zusammengewürfelte Farbe.
    image = np.dstack(bands) if len(bands) == 3 else bands[0]
    return image, facts


def write_png(path: str, out_path: str, max_px: int = MAX_PX) -> dict[str, Any] | None:
    """Dasselbe Bild als PNG neben das GeoTIFF legen — ``None``, wenn es nicht ging.

    Ein GeoTIFF ist ein Datenformat, kein Bild: Kein Chat-Kanal zeigt es, kein
    Sehmodell liest es, und im Browser bleibt es ein Download. Dieselbe Regel, nach
    der `render_map` neben die HTML-Karte ein flaches Bild legt (`mapoutput`,
    `_write_picture_beside`) — ein Artefakt in zwei Formen, nicht zwei Ergebnisse.

    Die georeferenzierten Zahlen bleiben im TIFF; das PNG ist zum **Hinsehen** da.
    """
    made = preview(path, max_px)
    if made is None:
        return None
    image, facts = made
    try:
        from PIL import Image

        Image.fromarray(image).save(out_path)
    except Exception:  # noqa: BLE001 - ein Bild nebenbei darf das Ergebnis nie kosten
        return None
    return facts
