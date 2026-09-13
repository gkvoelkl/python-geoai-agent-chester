"""Network reach without QGIS — the isochrone, on networkx.

Phase KQ step 3c (`internal/TODO.md`). The sibling of `geoops`/`rasterops`/
`terrainops`, and the replacement for `qgis_service_area`: the area reachable from a
point within a time budget **along the network**, not as the crow flies.

Why this matters more than it looks: a straight-line buffer overstates reach wherever
a river, a railway or a motorway cuts the fabric, and accessibility questions ("what is
reachable in 15 minutes on foot") are exactly where that error is largest. The
isochrone is the honest answer, and it is only honest if the graph really was used —
which is why the result reports how far the start point was from the network and how
much of the network it could reach.

Three traps are encoded:

* **A geographic CRS** turns a travel distance into degrees. Refused.
* **A start point off the network.** The nearest node may be a kilometre away, and the
  isochrone would then describe a different place than the one asked about. Reported
  in metres, and refused past 1 km.
* **A disconnected component.** Reaching 12 of 4000 nodes is not a 15-minute
  isochrone, it is a stranded fragment; the share is part of the answer.
"""

from __future__ import annotations

from typing import Any

from chester import provenance
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

#: Assumed speeds in km/h — the same table `qgis_service_area` uses, so a run that
#: switches paths does not silently switch its assumptions.
TRAVEL_SPEEDS_KMH = {"walk": 4.5, "bike": 15.0, "drive": 50.0}

#: Past this the start point is not "on" the network any more and the isochrone would
#: be about somewhere else.
_MAX_SNAP_M = 1000.0

#: Coordinates are rounded to this before becoming graph nodes, so two segments that
#: meet at a shared vertex actually share a node. Without it a road network falls
#: apart into thousands of one-edge components and every isochrone is a stub.
_SNAP_M = 0.01


def _node(x: float, y: float) -> tuple[float, float]:
    return (round(x / _SNAP_M) * _SNAP_M, round(y / _SNAP_M) * _SNAP_M)


def _build_graph(gdf):
    """A weighted graph from a line layer: vertices become nodes, segments edges."""
    import math

    import networkx as nx

    graph = nx.Graph()
    for geom in gdf.geometry:
        if geom is None or geom.is_empty:
            continue
        parts = geom.geoms if geom.geom_type.startswith("Multi") else [geom]
        for part in parts:
            coords = list(part.coords)
            for (x1, y1), (x2, y2) in zip(coords, coords[1:], strict=False):
                a, b = _node(x1, y1), _node(x2, y2)
                if a == b:
                    continue
                length = math.hypot(x2 - x1, y2 - y1)
                if not graph.has_edge(a, b) or graph[a][b]["weight"] > length:
                    graph.add_edge(a, b, weight=length)
    return graph


def service_area(network_path: str, output_path: str, *, start_lon: float,
                 start_lat: float, minutes: float, mode: str = "walk",
                 start_crs: str = "EPSG:4326",
                 workspace: str = DEFAULT_WORKSPACE) -> dict:
    """The area reachable from a start point within ``minutes`` along the network.

    ``network_path`` is a LINE layer in a **metric** CRS. ``start_lon``/``start_lat``
    are longitude then latitude in ``start_crs`` — named separately so the order
    cannot be swapped, and transformed to the network's CRS here.

    ``mode`` sets the assumed speed (walk 4.5, bike 15, drive 50 km/h); the budget
    becomes a network distance, so the result does not depend on anyone's idea of a
    time unit.
    """
    import geopandas as gpd
    import networkx as nx
    import shapely
    from shapely.geometry import Point

    speed = TRAVEL_SPEEDS_KMH.get(mode)
    if speed is None:
        return {"ok": False, "error": f"unknown mode {mode!r}; one of "
                                      f"{list(TRAVEL_SPEEDS_KMH)}"}
    gdf = gpd.read_file(resolve_path(network_path, workspace))
    if gdf.empty:
        return {"ok": False, "error": f"{network_path} holds no lines"}
    if gdf.crs is not None and gdf.crs.is_geographic:
        return {"ok": False, "error": (
            f"the network is in {gdf.crs}, a geographic CRS — a travel distance would "
            "be measured in degrees. Reproject it to a metric CRS first (EPSG:25832 "
            "for Germany, 2056 for Switzerland, 31287 for Austria).")}

    start = gpd.GeoSeries([Point(start_lon, start_lat)], crs=start_crs)
    if gdf.crs is not None:
        start = start.to_crs(gdf.crs)
    sx, sy = start.iloc[0].x, start.iloc[0].y

    graph = _build_graph(gdf)
    if not graph.number_of_nodes():
        return {"ok": False, "error": "the network has no usable segments"}
    nodes = list(graph.nodes)
    origin = min(nodes, key=lambda n: (n[0] - sx) ** 2 + (n[1] - sy) ** 2)
    snap_m = ((origin[0] - sx) ** 2 + (origin[1] - sy) ** 2) ** 0.5
    if snap_m > _MAX_SNAP_M:
        return {"ok": False, "error": (
            f"the nearest network node is {snap_m:.0f} m from the start point — this "
            "network does not cover that place, and an isochrone from here would "
            "describe somewhere else. Fetch a network around the actual start.")}

    budget_m = (minutes / 60.0) * speed * 1000.0
    reached = nx.single_source_dijkstra_path_length(graph, origin, cutoff=budget_m,
                                                    weight="weight")
    points = [Point(x, y) for x, y in reached]
    if len(points) < 3:
        return {"ok": False, "error": (
            f"only {len(points)} node(s) are within {minutes} min — too few for an "
            f"area, out of {graph.number_of_nodes()} in the network. Three causes, in "
            "order of likelihood: the layer is NOT NODED (its lines cross without "
            "sharing a vertex, so nothing connects — check whether segments end at "
            "intersections), the start sits on an isolated stub, or the budget is "
            "shorter than the first segment.")}
    hull = shapely.concave_hull(shapely.MultiPoint(points), ratio=0.3)
    out_gdf = gpd.GeoDataFrame(
        {"minutes": [minutes], "mode": [mode], "speed_kmh": [speed],
         "reach_m": [budget_m]},
        geometry=[hull], crs=gdf.crs)
    out = resolve_path(output_path, workspace, write=True)
    out_gdf.to_file(out)
    provenance.write_meta(out, source="chester", tool="service_area",
                          query=f"{minutes}min {mode}")

    share = len(reached) / graph.number_of_nodes()
    facts: dict[str, Any] = {
        "ok": True, "output": out, "minutes": minutes, "mode": mode,
        "speed_kmh": speed, "reach_m": round(budget_m),
        "nodes_total": graph.number_of_nodes(), "nodes_reached": len(reached),
        "snapped_m": round(snap_m, 1),
        "area_m2": round(float(hull.area)),
        # Der Vergleich, der die Aussage erst prüfbar macht: Wäre die Isochrone so
        # groß wie ein Luftlinienkreis, hat das Netz nichts beigetragen — dann ist
        # entweder das Netz zu grob oder die Antwort ist ein verkleideter Puffer.
        "straight_line_circle_m2": round(3.14159 * budget_m ** 2),
    }
    if share < 0.02 and graph.number_of_nodes() > 100:
        facts["warning"] = (
            f"only {len(reached)} of {graph.number_of_nodes()} network nodes were "
            f"reachable ({share:.1%}) — the start probably sits on a component cut "
            "off from the rest (a footpath island, a network clipped too tightly). "
            "The isochrone describes that fragment, not the neighbourhood.")
    elif facts["area_m2"] > 0.9 * facts["straight_line_circle_m2"]:
        facts["warning"] = (
            "the isochrone is nearly as large as a straight-line circle of the same "
            "reach — the network imposed almost no detour. Check that the layer "
            "really is the street network and not, say, a single long road.")
    return facts


#: One entry, same shape as the other three modules.
OPERATIONS = {"service_area": service_area}
