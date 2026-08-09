---
name: lidar-ground
description: Classify ground points in a LiDAR point cloud (LAS/LAZ) and rasterize them into a digital terrain model (DTM). LiDAR workflow using the QGIS PDAL provider.
version: 1
---

# LiDAR ground & DTM

From a raw LiDAR point cloud (LAS/LAZ), classify the ground returns and turn them
into a digital terrain model (DTM) raster — the basis for slope, hydrology and
building-height work.

Requires the QGIS **PDAL** provider (algorithms with the `pdal:` prefix). Check it
is available with `qgis_search("pdal ground")`; if nothing is found, the provider
is not installed.

## Inputs (ask the user for any that are missing)
- `pointcloud` — a LAS/LAZ file. **Optional**: if the user names a place/area
  instead, fetch one (step 0).
- `resolution` — output DTM cell size in metres (default 1.0)
- a working directory for outputs (default the workspace)

## Steps
0. **Get a point cloud if none was given.** `pointcloud_search(bbox)` lists the
   LiDAR datasets covering the area (OpenTopography). Pick one, get its **tile
   index** URL from its landing page, then
   `fetch_pointcloud(bbox, tile_index_url)` downloads the intersecting LAZ tiles
   into the cache. Skip when the user provides a cloud. (Point clouds are large —
   keep the bbox tight.)
1. **Inspect.** `qgis_describe("pdal:classifyground")` to confirm parameters
   (input, output point cloud, optional ground-detection settings).
2. **Classify ground.** `qgis_run("pdal:classifyground", {"INPUT": pointcloud,
   "OUTPUT": ".../classified.laz"})` — tags ground vs non-ground returns.
3. **Rasterize to DTM.** Find the export algorithm — `qgis_search("pdal export
   raster")` (e.g. `pdal:exportrastertin` or `pdal:exportraster`) — then run it on
   the classified cloud, filtering to ground points, at the requested resolution,
   writing ".../dtm.tif".
4. **Validate.** `sanity_check_result(".../dtm.tif")` — confirm the DTM raster has a
   CRS, the expected size and sensible elevation bounds.

## Notes
- Point clouds are large; work on a clipped area of interest where possible
  (`pdal:clip`) before classifying.
- The resulting DTM feeds directly into the terrain-analysis and building-heights
  skills.

## Report
State the input cloud, the ground-classification algorithm used, the DTM
resolution/CRS, and the output file paths.

> Status: workflow defined against the installed PDAL provider; not yet run
> end-to-end here (no sample LAS/LAZ in the repo).
