---
name: terrain-analysis
description: Derive slope, aspect, hillshade and ruggedness from a digital terrain model (DTM/DEM) raster. Raster terrain workflow.
version: 1
---

# Terrain analysis

Compute standard terrain derivatives from a digital terrain model (DTM/DEM)
raster: slope (steepness), aspect (direction a slope faces), hillshade (shaded
relief) and optionally ruggedness.

## Inputs (ask the user for any that are missing)
- `dtm` — digital terrain model raster (elevation). **Optional**: if the user only
  names a place/area, fetch one automatically (step 0).
- which products are wanted (default: slope + hillshade)
- a working directory for outputs (default the workspace)

## Steps
0. **Get a DEM if none was given.** If the user supplies no `dtm` but names a place
   or bbox: `geocode(place)` for a bbox if needed, then
   `fetch_dem(bbox, output_path=".../dem.tif")` — Copernicus GLO-30 (~30 m)
   elevation. Its output is **EPSG:4326 (degrees)** and must be reprojected in
   step 1 before any slope computation. Skip this step when the user provides a DEM.
1. **Check the input.** `check_crs(dtm)` — it must have a CRS. For slope to be in
   degrees, the elevation unit and horizontal unit must be consistent (a projected
   metre-based CRS is the safe case). A `fetch_dem` result (or any geographic DEM)
   is in degrees, so reproject it to a metric CRS first (`qgis_reproject` works on
   rasters too via the generic path, or use `gdal:warpreproject`) — pick a CRS
   suitable for the area (e.g. EPSG:25832 for Germany, or the local UTM zone).
2. **Slope.** `qgis_run("native:slope", {"INPUT": dtm, "OUTPUT": ".../slope.tif"})`
   — degrees of steepness per cell.
3. **Aspect** (if wanted). `qgis_run("native:aspect", {"INPUT": dtm,
   "OUTPUT": ".../aspect.tif"})` — compass direction the slope faces (0–360°).
4. **Hillshade** (if wanted). `qgis_run("native:hillshade", {"INPUT": dtm,
   "OUTPUT": ".../hillshade.tif"})` — shaded relief for visualization.
5. **Ruggedness** (optional). `qgis_run("native:ruggednessindex", {"INPUT": dtm,
   "OUTPUT": ".../tri.tif"})`.
6. **Validate.** `sanity_check_result(".../slope.tif")` — confirm the output raster
   has the expected size/CRS and is non-empty. Slope values should fall in 0–90°.

If you are unsure of an algorithm's parameters, use `qgis_describe` first (e.g.
`qgis_describe("native:slope")`) — some accept a Z_FACTOR for vertical exaggeration.

## Notes
- Outputs are rasters; `render_map` renders vector layers only, so report the
  raster file paths rather than mapping them.
- Slope in degrees assumes matching horizontal/vertical units; if the DTM is in
  degrees (geographic), reproject first or the slope values will be wrong.

## Report
List the products created with their file paths, the DTM's CRS, and the slope value
range from the validation step.
