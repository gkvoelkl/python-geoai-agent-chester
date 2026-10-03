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


def test_an_areal_way_does_not_kill_the_graph(tmp_path):
    """A pedestrian square in the network is a Polygon — and was the real crash.

    Measured 2026-09-21 in Regensburg: 595 LineStrings and one Polygon (the cathedral
    square), and `part.coords` raised `NotImplementedError`. The rim of such an area
    is walkable and becomes edges; this test holds that it really contributes rather
    than being silently skipped.
    """
    from shapely.geometry import Polygon

    _ws(tmp_path)
    x0, y0 = 700000.0, 5400000.0
    # A stub path ends on a vertex of the square — noded, the way OSM delivers it;
    # beyond that point the rim is the only way on.
    geoms = [LineString([(x0, y0), (x0 + 100, y0)]),
             Polygon([(x0 + 100, y0), (x0 + 300, y0), (x0 + 300, y0 + 100),
                      (x0 + 100, y0 + 100)])]
    gpd.GeoDataFrame({"id": [0, 1]}, geometry=geoms, crs="EPSG:25832").to_file(
        tmp_path / "geocache" / "square.gpkg")

    res = N.service_area("square.gpkg", "iso.gpkg", start_lon=x0, start_lat=y0,
                         minutes=10, start_crs="EPSG:25832", workspace=_ws(tmp_path))
    assert res["ok"] is True, res
    # Without the rim the graph would stop at x0+100 and have no area. With it, the
    # four corners of the 200x100 square plus the path start: 20000 m2 for the square
    # and a 5000 m2 triangle back to the stub — checkable by hand.
    assert res["nodes_total"] == 5, res["nodes_total"]
    assert res["area_m2"] == 25000, res["area_m2"]


def test_a_budget_that_never_bound_says_so(tmp_path):
    """If the isochrone covers the whole island, it describes the network, not the time."""
    _ws(tmp_path)
    x0, y0 = 700000.0, 5400000.0
    island = LineString([(x0, y0), (x0 + 50, y0), (x0 + 50, y0 + 50), (x0, y0 + 50),
                         (x0, y0)])
    far = LineString([(x0 + 5000, y0), (x0 + 5300, y0)])
    gpd.GeoDataFrame({"id": [0, 1]}, geometry=[island, far], crs="EPSG:25832").to_file(
        tmp_path / "geocache" / "island.gpkg")

    res = N.service_area("island.gpkg", "iso.gpkg", start_lon=x0, start_lat=y0,
                         minutes=10, start_crs="EPSG:25832", workspace=_ws(tmp_path))
    assert res["ok"] is True, res
    assert res["budget_bound"] is False
    assert "never bound" in res["warning"]


def test_a_budget_that_did_bind_carries_no_warning(tmp_path):
    """The counter-check — otherwise the tool always warns and the warning goes deaf."""
    net = _grid(tmp_path)
    res = N.service_area(net, "iso.gpkg", start_lon=700500.0, start_lat=5400500.0,
                         minutes=10, start_crs="EPSG:25832", workspace=_ws(tmp_path))
    assert res["ok"] is True
    assert res["budget_bound"] is True
    assert "never bound" not in res.get("warning", "")


def test_the_reachable_set_matches_an_analytic_oracle(tmp_path):
    """Dijkstra against a reach that is counted by hand, not by a second library.

    On the regular grid every distance is Manhattan, so the reachable set is exactly
    the nodes within `budget // step` steps of the start — 97 of 121 for a 750 m
    budget on a 100 m grid. This is the correctness floor for `service_area`: it needs
    no QGIS, no GRASS and no network, so it holds on any machine.

    A cross-check against GRASS `v.net.iso` on the real Regensburg road network (21.242
    arcs, identical input, 2026-09-22) put the two implementations 0,51 % apart —
    reassuring, but that measurement cannot live in a test, and an oracle that is only
    available on one machine is no oracle.
    """
    step, budget_steps = 100.0, 7          # 750 m budget, 8 steps would be 800 m
    centre = 5                             # _grid builds 11x11 from (700000, 5400000)
    expected = sum(1 for i in range(11) for j in range(11)
                   if abs(i - centre) + abs(j - centre) <= budget_steps)

    net = _grid(tmp_path, step=step)
    res = N.service_area(net, "iso.gpkg",
                         start_lon=700000.0 + centre * step,
                         start_lat=5400000.0 + centre * step,
                         minutes=10, start_crs="EPSG:25832", workspace=_ws(tmp_path))
    assert res["ok"] is True, res
    assert expected == 97, expected               # the oracle itself, spelled out
    assert res["nodes_reached"] == expected, res["nodes_reached"]
