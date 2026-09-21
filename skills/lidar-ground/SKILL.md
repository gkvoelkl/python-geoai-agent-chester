---
name: lidar-ground
description: Get a ground-classified terrain model (DTM) — the official 1 m DGM for DACH, or ground classification from a raw LiDAR point cloud where no official product exists.
version: 1
---

# LiDAR ground & DTM

Produce a **digital terrain model** — bare earth, no buildings or vegetation — as the
basis for slope, hydrology and building-height work.

**Take the official DTM first.** For the focus region the ground classification has
already been done, by the surveying authority, on denser data than you will fetch:

- Germany: `fetch_dgm1(bbox, output_path=".../dtm.tif")` — open 1 m DGM1.
- Switzerland: `fetch_swissalti3d(bbox, …)` · Austria: `fetch_austria_dem(bbox, …)`.
- Anywhere else, coarser: `fetch_dem(bbox, …)` — Copernicus GLO-30 (~30 m).

Classifying a raw cloud yourself is the route for **user-supplied point clouds** and
for areas no official product covers. It is slower, it needs a tool Chester does not
always have (below), and its result is worse than the official one for the same area.
Say which route you took in the answer — a 1 m DGM1 and a self-classified DTM are not
the same evidence.

## What Chester can and cannot do with a raw cloud

Without QGIS: find and download clouds (`pointcloud_search`, `fetch_pointcloud`).
**Ground classification and rasterising to a DTM need QGIS with the PDAL provider**,
which this installation may not have. If it is missing, there is no substitute in the
toolbox: fall back to the official DTM above, or say the analysis cannot be done —
never pass a first-return surface (DSM) off as terrain.

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
1. **Make it loadable** (optional, for viewing): `pointcloud_to_copc(input_path)`
   converts LAS/LAZ to COPC. Also needs QGIS.
2. **Classify ground and rasterize.** Needs the QGIS PDAL provider (see above). If it
   is unavailable, stop here and take the official DTM route.
3. **Validate.** `sanity_check_result(".../dtm.tif")` — confirm the DTM raster has a
   CRS, the expected size and sensible elevation bounds. Compare a few elevations
   against a known value; a DTM that sits metres above the ground is a DSM.

## Notes
- Point clouds are large; work on a clipped area of interest where possible.
- The resulting DTM feeds directly into the terrain-analysis and building-heights
  skills — as does `fetch_dgm1`, which is why the official route is the default.
