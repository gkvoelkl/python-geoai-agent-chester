"""Generate the fixtures for Test-Level 2 (micro geo tasks) — reproducibly.

Every expected value of the probe tasks is computed and printed **here**, so it stays
traceable instead of merely asserted. Run:

    uv run python probes/make_fixtures.py

Writes to ``probes/fixtures/`` (about 1.1 MB, checked in). The geometries lie around
Regensburg in EPSG:25832; where a task tests the degrees trap, the same layer is also
written in EPSG:4326.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

OUT = Path(__file__).parent / "fixtures"
CRS = "EPSG:25832"
X0, Y0 = 720_000, 5_430_000  # somewhere near Regensburg, round numbers
expected: dict[str, float] = {}


def _write(gdf: gpd.GeoDataFrame, name: str) -> None:
    gdf.to_file(OUT / name)
    print(f"  {name:34s} {len(gdf)} Objekte, {gdf.crs.to_string()}")


def schools() -> None:
    """Three points, far enough apart that 500 m buffers do not touch."""
    pts = [Point(X0 + i * 3000, Y0) for i in range(3)]
    gdf = gpd.GeoDataFrame({"name": ["A", "B", "C"]}, geometry=pts, crs=CRS)
    _write(gdf.to_crs(4326), "schools_4326.gpkg")
    expected["buffer_500m_area_m2"] = 3 * math.pi * 500**2


def parcel() -> None:
    """Ein 200×200-m-Quadrat — die Fläche, die in Grad gemessen 4e-8 ergäbe."""
    poly = box(X0, Y0, X0 + 200, Y0 + 200)
    gdf = gpd.GeoDataFrame({"name": ["Flurstück"]}, geometry=[poly], crs=CRS)
    _write(gdf.to_crs(4326), "parcel_4326.gpkg")
    expected["parcel_area_m2"] = poly.area


def road_and_green() -> None:
    """Road + three green areas: selecting gives exactly three times the intersection.

    The road lies on y=Y0, the areas from y+50 to y+200 (300 m wide). A 100 m buffer
    reaches y+100, so it cuts 50 m of the 150 m height.
    """
    road = LineString([(X0 - 500, Y0), (X0 + 3500, Y0)])
    _write(gpd.GeoDataFrame({"name": ["B8"]}, geometry=[road], crs=CRS), "road.gpkg")

    greens = [box(X0 + i * 1000, Y0 + 50, X0 + i * 1000 + 300, Y0 + 200) for i in range(3)]
    _write(
        gpd.GeoDataFrame({"name": ["Park A", "Park B", "Wald C"]}, geometry=greens, crs=CRS),
        "green.gpkg",
    )
    buf = road.buffer(100)
    expected["green_intersection_area_m2"] = sum(g.intersection(buf).area for g in greens)
    expected["green_whole_area_m2"] = sum(g.area for g in greens)  # die Falle: 3×


def overlapping_zones() -> None:
    """Three overlapping squares — sum and union differ widely."""
    squares = [box(X0 + i * 150, Y0, X0 + i * 150 + 200, Y0 + 200) for i in range(3)]
    _write(
        gpd.GeoDataFrame({"name": ["Z1", "Z2", "Z3"]}, geometry=squares, crs=CRS),
        "zones.gpkg",
    )
    expected["zones_union_area_m2"] = unary_union(squares).area
    expected["zones_sum_area_m2"] = sum(s.area for s in squares)


def boundary_points() -> None:
    """Four points, one exactly on the edge — `within` and `intersects` part ways."""
    poly = box(X0, Y0, X0 + 1000, Y0 + 1000)
    _write(gpd.GeoDataFrame({"name": ["Bezirk"]}, geometry=[poly], crs=CRS), "district.gpkg")
    pts = [
        Point(X0 + 200, Y0 + 200),
        Point(X0 + 500, Y0 + 500),
        Point(X0 + 800, Y0 + 300),
        Point(X0 + 1000, Y0 + 500),  # exactly on the edge
    ]
    _write(
        gpd.GeoDataFrame({"name": ["P1", "P2", "P3", "P4-Kante"]}, geometry=pts, crs=CRS),
        "stops.gpkg",
    )
    expected["points_strictly_within"] = sum(1 for p in pts if p.within(poly))


def ags_join() -> None:
    """AGS as an integer in the layer, as a string with a leading zero in the table."""
    codes = ["09375117", "09375163", "09362000", "09271000"]
    polys = [box(X0 + i * 500, Y0 + 2000, X0 + i * 500 + 400, Y0 + 2400) for i in range(4)]
    namen = ["Barbing", "Pentling", "Regensburg", "Deggendorf"]
    gdf = gpd.GeoDataFrame(
        {"ags": [int(c) for c in codes], "name": namen}, geometry=polys, crs=CRS
    )
    _write(gdf, "gemeinden.gpkg")

    with open(OUT / "einwohner.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ags", "einwohner"])
        for c, n in zip(codes, [5482, 4108, 152610, 33636], strict=True):
            w.writerow([c, n])  # führende Null bleibt erhalten
    print(f"  {'einwohner.csv':34s} 4 Zeilen, AGS als String mit führender Null")
    expected["join_rows"] = len(codes)


def address_table() -> None:
    """Four geocoded addresses as a table — the route from coordinates to a layer.

    From practice: "mark these four buildings" is the most common small task, and it
    always starts the same way — geocode, then make a layer from the coordinates. That
    is exactly where the tool route broke (2026-09-01), and no probe covered it.
    """
    rows = [("Domplatz 7", 12.096918, 49.019369), ("Residenzstrasse 2", 12.096883, 49.019167),
            ("Rathausplatz 4", 12.094158, 49.020215), ("Neue-Waag-Gasse 2", 12.093799, 49.020036)]
    with open(OUT / "adressen.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "lon", "lat"])
        w.writerows(rows)
    print(f"  {'adressen.csv':34s} {len(rows)} Adressen mit lon/lat, keine Geometriespalte")
    expected["address_points"] = len(rows)


def rgb_without_nir() -> None:
    """An aerial image with three bands — no NDVI possible, and that is the test."""
    path = OUT / "aerial_rgb.tif"
    rng = np.random.default_rng(20260829)
    data = rng.integers(40, 210, size=(3, 200, 200), dtype="uint16")
    with rasterio.open(
        path, "w", driver="GTiff", height=200, width=200, count=3, dtype="uint16",
        crs=CRS, transform=from_origin(X0, Y0 + 200, 0.2, 0.2),
    ) as dst:
        dst.write(data)
    print(f"  {'aerial_rgb.tif':34s} 3 Banden (R,G,B), 20 cm, kein Infrarot")


def buildings() -> None:
    """Buildings with known footprints and heights — the two migrated cases from the bank."""
    specs = [("Klein", 20, 20, 8.0), ("Mittel", 24, 24, 18.0), ("Hoch", 30, 20, 25.0)]
    polys, names, heights = [], [], []
    for i, (name, w, h, height) in enumerate(specs):
        polys.append(box(X0 + i * 100, Y0 + 3000, X0 + i * 100 + w, Y0 + 3000 + h))
        names.append(name)
        heights.append(height)
    gdf = gpd.GeoDataFrame({"name": names, "hoehe_m": heights}, geometry=polys, crs=CRS)
    _write(gdf, "buildings.gpkg")
    expected["building_footprint_area_m2"] = sum(p.area for p in polys)

    # Gini over the heights: mean absolute difference / (2 × mean).
    # A name of its own — `h` above is already the footprint's edge length.
    hs = np.array(heights)
    diffs = np.abs(hs[:, None] - hs[None, :]).sum()
    expected["building_height_gini"] = float(diffs / (2 * len(hs) ** 2 * hs.mean()))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Schreibe Fixtures nach {OUT}/")
    for fn in (schools, parcel, road_and_green, overlapping_zones,
               boundary_points, ags_join, address_table, rgb_without_nir, buildings):
        fn()
    (OUT / "expected.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("\nSollwerte (auch in probes/fixtures/expected.json):")
    for k, v in sorted(expected.items()):
        print(f"  {k:34s} {v:,.4f}" if isinstance(v, float) else f"  {k:34s} {v}")


if __name__ == "__main__":
    main()
