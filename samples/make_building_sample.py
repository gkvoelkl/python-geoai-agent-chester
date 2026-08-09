"""Generate a small, reproducible sample dataset for the building-heights workflow.

Produces, in EPSG:25832 (metric), under samples/building_heights/:
    buildings.geojson  — 3 building footprints (true heights 8 / 18 / 25 m)
    roads.geojson      — 2 streets near the buildings
    dtm.tif            — digital terrain model (flat, 50 m)
    dsm.tif            — surface model (terrain + building heights)

So DSM - DTM recovers each building's height, of which two exceed 15 m.

Run:  uv run python samples/make_building_sample.py
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.transform import from_origin
from shapely.geometry import LineString, box

CRS = "EPSG:25832"
OUT = Path(__file__).parent / "building_heights"

# (name, footprint, true height in metres)
BUILDINGS = [
    ("Haus Klein", box(500000, 5600000, 500020, 5600020), 8.0),
    ("Haus Mittel", box(500060, 5600040, 500084, 5600064), 18.0),
    ("Haus Hoch", box(499960, 5600060, 499990, 5600080), 25.0),
]
ROADS = [
    ("Hauptstrasse", LineString([(499950, 5600030), (500120, 5600030)])),
    ("Nebenweg", LineString([(500050, 5599980), (500050, 5600100)])),
]

# Raster grid covering all features, 1 m resolution.
MINX, MINY, MAXX, MAXY = 499940, 5599980, 500120, 5600120
RES = 1.0
TERRAIN_ELEV = 50.0


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    buildings = gpd.GeoDataFrame(
        {"name": [b[0] for b in BUILDINGS], "true_height": [b[2] for b in BUILDINGS]},
        geometry=[b[1] for b in BUILDINGS],
        crs=CRS,
    )
    buildings.to_file(OUT / "buildings.geojson")

    roads = gpd.GeoDataFrame(
        {"name": [r[0] for r in ROADS]},
        geometry=[r[1] for r in ROADS],
        crs=CRS,
    )
    roads.to_file(OUT / "roads.geojson")

    width = int((MAXX - MINX) / RES)
    height = int((MAXY - MINY) / RES)
    transform = from_origin(MINX, MAXY, RES, RES)

    dtm = np.full((height, width), TERRAIN_ELEV, dtype="float32")

    # Burn each building's height into a surface-above-terrain raster.
    above = rasterize(
        [(geom, h) for _, geom, h in BUILDINGS],
        out_shape=(height, width),
        transform=transform,
        fill=0.0,
        dtype="float32",
    )
    dsm = dtm + above

    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "width": width,
        "height": height,
        "crs": CRS,
        "transform": transform,
        "nodata": -9999.0,
    }
    for name, data in (("dtm.tif", dtm), ("dsm.tif", dsm)):
        with rasterio.open(OUT / name, "w", **profile) as ds:
            ds.write(data, 1)

    print(f"Wrote sample dataset to {OUT}/")
    print(f"  buildings: {len(buildings)} (heights {[b[2] for b in BUILDINGS]}, >15 m: 2)")
    print(f"  roads:     {len(roads)}")
    print(f"  rasters:   dtm.tif, dsm.tif  ({width}x{height} px @ {RES} m, {CRS})")


if __name__ == "__main__":
    main()
