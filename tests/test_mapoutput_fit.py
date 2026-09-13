"""Eine Rasterkarte zoomt auf ihre Daten — sonst ist sie leer.

Gemessen 2026-09-07 (`swiss-terrain-slope-grindelwald`): Das Hangneigungsraster deckte
1,1 x 0,8 km ab. `render_map` setzte bei einem Raster als Basisebene nur den
Mittelpunkt und liess `zoom_start` weg, folium nahm seine Vorgabe **10** — rund 50 km
Blickfeld, das Raster ein paar Pixel gross. Die Judges lasen die Rueckgabe, fanden die
Rechnung richtig und gaben „passt"; auf der Karte war nichts zu sehen.

Vektorkarten trifft es nicht, `gdf.explore()` zoomt selbst auf seine Daten — genau
deshalb ist der Fehler nur im reinen Rasterfall so lange durchgerutscht. Er zeigt
zugleich, warum die Hausregel „das Artefakt ansehen, nicht die Rueckgabe" auch fuer
die Bank gilt: `ok: true` und ein plausibler PNG-Anhang haben ihn zugedeckt.
"""

from __future__ import annotations

import re

import numpy as np
import rasterio
from _util import tools_of
from rasterio.transform import from_origin

from chester.capabilities.mapoutput import MapOutputCapability

#: Ungefaehr der Ausschnitt aus dem Lauf: ein Kilometer im Berner Oberland.
_SWISS_ORIGIN = (2645000, 1168000)


def _tiny_raster(tmp_path, name="slope.tif", px=200, res=5):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    arr = (np.random.default_rng(0).random((px, px)) * 40).astype("float32")
    with rasterio.open(tmp_path / "geocache" / name, "w", driver="GTiff",
                       height=px, width=px, count=1, dtype="float32",
                       crs="EPSG:2056",
                       transform=from_origin(*_SWISS_ORIGIN, res, res)) as dst:
        dst.write(arr, 1)
    return name


def _fit_bounds(html: str):
    """Die Grenzen, auf die die fertige Karte zoomt — oder ``None``."""
    m = re.search(r"fitBounds\(\s*\[\[([\d.eE+-]+),\s*([\d.eE+-]+)\],\s*"
                  r"\[([\d.eE+-]+),\s*([\d.eE+-]+)\]\]", html)
    return [float(g) for g in m.groups()] if m else None


def test_a_raster_only_map_zooms_to_the_raster(tmp_path):
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    res = tools["render_map"](layers=[_tiny_raster(tmp_path)], output_path="m.html")
    assert res["ok"]
    html = (tmp_path / "geocache" / "m.html").read_text(encoding="utf-8")

    fit = _fit_bounds(html)
    assert fit is not None, "ohne fitBounds oeffnet folium auf Zoom 10 — leere Karte"

    # Und zwar auf *dieses* Raster: die Grenzen muessen die des Overlays sein.
    # Das Bild ist eine data-URI in Anfuehrungszeichen und enthaelt selbst ein
    # Komma ("...;base64,") — die Grenzen stehen erst hinter dem schliessenden ".
    overlay = re.search(r'imageOverlay\(\s*"[^"]*",\s*\[\[([\d.eE+-]+),\s*([\d.eE+-]+)\],\s*'
                        r'\[([\d.eE+-]+),\s*([\d.eE+-]+)\]\]', html, re.S)
    assert overlay, "kein ImageOverlay im HTML"
    assert fit == [float(g) for g in overlay.groups()]


def test_the_fitted_extent_is_the_kilometre_it_should_be(tmp_path):
    """Der Zahlenbeleg: ein Kilometer, nicht fuenfzig."""
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    tools["render_map"](layers=[_tiny_raster(tmp_path)], output_path="m.html")
    south, west, north, east = _fit_bounds(
        (tmp_path / "geocache" / "m.html").read_text(encoding="utf-8"))
    height_km = (north - south) * 111.0
    width_km = (east - west) * 111.0 * 0.69  # cos(46.6 Grad)
    assert 0.8 < height_km < 1.3, height_km
    assert 0.8 < width_km < 1.3, width_km


def test_two_rasters_are_both_in_view(tmp_path):
    """Mehrere Raster: die Vereinigung, nicht das erste."""
    a = _tiny_raster(tmp_path, "a.tif")
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    arr = np.ones((200, 200), dtype="float32")
    with rasterio.open(tmp_path / "geocache" / "b.tif", "w", driver="GTiff",
                       height=200, width=200, count=1, dtype="float32", crs="EPSG:2056",
                       transform=from_origin(_SWISS_ORIGIN[0] + 1000,
                                             _SWISS_ORIGIN[1], 5, 5)) as dst:
        dst.write(arr, 1)
    tools = tools_of(MapOutputCapability(workspace=str(tmp_path)))
    tools["render_map"](layers=[a, "b.tif"], output_path="m.html")
    south, west, north, east = _fit_bounds(
        (tmp_path / "geocache" / "m.html").read_text(encoding="utf-8"))
    assert (east - west) * 111.0 * 0.69 > 1.8, "beide Kacheln muessen hineinpassen"
