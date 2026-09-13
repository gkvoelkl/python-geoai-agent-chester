"""Netzwerk-Erreichbarkeit ohne QGIS (`chester/networkops.py`) — offline, deterministisch.

Phase KQ Schritt 3c. Eine Isochrone ist nur dann eine ehrliche Antwort, wenn das Netz
wirklich benutzt wurde; die Tests pruefen genau das und die drei Fallen drumherum.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import LineString

from chester import networkops as N


def _ws(tmp_path):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    return str(tmp_path)


def _grid(tmp_path, name="net", n=11, step=100.0, crs="EPSG:25832", x0=700000.0,
          y0=5400000.0):
    """Ein regelmaessiges Strassenraster — jede Distanz ist von Hand nachrechenbar."""
    _ws(tmp_path)
    # Stuetzpunkte an JEDER Kreuzung — echte Netzdaten sind genodet, und ohne das
    # zerfaellt der Graph in sich kreuzende, aber unverbundene Linien.
    lines = []
    for i in range(n):
        row = [(x0 + j * step, y0 + i * step) for j in range(n)]
        col = [(x0 + i * step, y0 + j * step) for j in range(n)]
        lines.append(LineString(row))
        lines.append(LineString(col))
    gpd.GeoDataFrame({"id": range(len(lines))}, geometry=lines, crs=crs).to_file(
        tmp_path / "geocache" / f"{name}.gpkg")
    return f"{name}.gpkg"


def test_the_reach_follows_the_speed_table(tmp_path):
    """10 Minuten zu Fuss bei 4,5 km/h sind 750 m — nachrechenbar, nicht geraten."""
    net = _grid(tmp_path)
    res = N.service_area(net, "iso.gpkg", start_lon=700500.0, start_lat=5400500.0,
                         minutes=10, mode="walk", start_crs="EPSG:25832",
                         workspace=_ws(tmp_path))
    assert res["ok"] is True, res.get("error")
    assert res["reach_m"] == 750 and res["speed_kmh"] == 4.5


def test_the_isochrone_is_smaller_than_a_straight_line_circle(tmp_path):
    """Die Zahl, die die Aussage pruefbar macht.

    Auf einem Raster muss man um Ecken laufen; die erreichte Flaeche ist deshalb
    kleiner als ein Kreis derselben Reichweite. Waere sie es nicht, waere die
    Isochrone ein verkleideter Puffer — und die Rueckgabe stellt beide Zahlen
    nebeneinander, damit das auffaellt.
    """
    net = _grid(tmp_path)
    res = N.service_area(net, "iso.gpkg", start_lon=700500.0, start_lat=5400500.0,
                         minutes=5, mode="walk", start_crs="EPSG:25832",
                         workspace=_ws(tmp_path))
    assert res["area_m2"] < res["straight_line_circle_m2"]


def test_a_geographic_network_is_refused(tmp_path):
    """Eine Reisedistanz in Grad gibt es nicht."""
    net = _grid(tmp_path, crs="EPSG:4326", x0=12.0, y0=49.0, step=0.001)
    res = N.service_area(net, "iso.gpkg", start_lon=12.005, start_lat=49.005,
                         minutes=10, workspace=_ws(tmp_path))
    assert res["ok"] is False
    assert "geographic CRS" in res["error"] and "25832" in res["error"]


def test_a_start_point_off_the_network_is_refused(tmp_path):
    """Sonst beschreibt die Isochrone einen anderen Ort als den gefragten."""
    net = _grid(tmp_path)
    res = N.service_area(net, "iso.gpkg", start_lon=900000.0, start_lat=5400500.0,
                         minutes=10, start_crs="EPSG:25832", workspace=_ws(tmp_path))
    assert res["ok"] is False
    assert "from the start point" in res["error"]
    assert "describe somewhere else" in res["error"]


def test_an_unknown_mode_names_the_known_ones(tmp_path):
    net = _grid(tmp_path)
    res = N.service_area(net, "iso.gpkg", start_lon=700500.0, start_lat=5400500.0,
                         minutes=10, mode="helicopter", start_crs="EPSG:25832",
                         workspace=_ws(tmp_path))
    assert res["ok"] is False and "walk" in res["error"]


def test_the_snap_distance_is_part_of_the_answer(tmp_path):
    """Wie weit der Start vom Netz weg lag, gehoert in die Antwort, nicht in eine Fussnote."""
    net = _grid(tmp_path)
    res = N.service_area(net, "iso.gpkg", start_lon=700530.0, start_lat=5400500.0,
                         minutes=10, start_crs="EPSG:25832", workspace=_ws(tmp_path))
    assert res["ok"] is True
    assert 25 <= res["snapped_m"] <= 35, res["snapped_m"]
