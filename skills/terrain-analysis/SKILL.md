---
name: terrain-analysis
description: Derive slope, aspect, hillshade and ruggedness from a digital terrain model (DTM/DEM) raster, and trace surface water with sink filling and flow accumulation. Raster terrain workflow.
version: 2
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
   is in degrees, so reproject it to a metric CRS first — pick a CRS suitable for
   the area (e.g. EPSG:25832 for Germany, or the local UTM zone).
2. **Slope.** `slope(dem_path=dtm, output_path=".../slope.tif")` — degrees of
   steepness per cell.
3. **Aspect** (if wanted). `aspect(dem_path=dtm, output_path=".../aspect.tif")` —
   compass direction the slope faces (0–360°).
4. **Hillshade** (if wanted). `hillshade(dem_path=dtm, output_path=".../hillshade.tif")`
   — shaded relief for visualization; `azimuth` and `altitude` set the light.
5. **Ruggedness** (optional). `ruggedness(dem_path=dtm, output_path=".../tri.tif")`.
6. **Validate.** `sanity_check_result(".../slope.tif")` — confirm the output raster
   has the expected size/CRS and is non-empty. Slope values should fall in 0–90°.

## Hydrology: where surface water collects

Use this part when the question is about **water collecting or running off** —
flow paths, thalwegs, pluvial ("Starkregen") collection lines, catchments.

Slope answers where water *runs*; **flow accumulation** answers where it *comes
together*. Only the second answers this question, and hillshade or contour lines do
not answer it at all. Do not substitute one for the other.

### H1. Take a DEM that extends **beyond** the area of interest
Water enters the area from uphill outside it. A DEM clipped to the boundary first has
that slope removed, so the computation is simply of something else. Fetch the DEM for
a **buffered** extent (roughly 1 km, more where the terrain above is steep) and clip
**at the very end**, for display only.

This is the one place where the general rule "named area → clip to the boundary"
must be applied last instead of first. The buffer is cheap: measured 19 s for 45 M
cells against 10 s for 22 M.

### H2. Fill sinks
`fill_sinks(dem_path=dem, output_path=".../filled.tif")`
Real DTMs contain pits; without filling, flow paths terminate in artefacts. **Needs
GRASS** (`r.fill.dir`); if GRASS is missing the tool says so, and the hydrology part
of this skill cannot be done here.

### H3. Flow accumulation
`flow_accumulation(dem_path=".../filled.tif", output_path=".../acc.tif")`
Feed it the **filled** DEM from H2. Values count upslope cells, not litres.

**Why the numbers are positive, and what it cost to learn.** The tool runs GRASS
`r.watershed` with the `-a` flag, and that flag is not decoration. Without it,
`r.watershed` writes every cell whose catchment reaches past the computation edge as a
*negative* number. Measured on a 2000×2000 test surface fed from one side: 100 % of
cells negative, so a plain `acc > 5000` selected **zero** cells — a blank map returned
as `ok: true` with a file path. With `-a` the same threshold selected 46,992 cells.

What `-a` means literally is "use positive accumulation even for likely
underestimates": edge-fed cells stay underestimated, the flag only stops them being
signed. That underestimate is exactly what the buffer in H1 shrinks — the two steps
belong together.

**Selecting the channels.** `flow_accumulation` returns the raw count, so pick the
threshold yourself: `raster_calc(output_path=".../streams.tif", expression="A > 5000",
a=".../acc.tif")`. What counts as a channel depends on cell size and catchment — look
at the result before you believe a threshold.

If `fill_sinks` or `flow_accumulation` report that GRASS is missing, the hydrology
part cannot be done on this machine. There is no substitute for accumulation: say the
analysis cannot be done, rather than passing slope off as an answer.

### H4. Turn the raster into lines
There is no vectorising tool in the toolbox. Go through `geo_python_run` with
`rasterio.features.shapes` over the thresholded raster, or leave the result as a
raster and say so — a stream raster answers "where do the lines run" perfectly well
on a map.

Do **not** reach for `gdal:polygonize` → `native:polygonstolines`. That traces the
outline of every pixel group, so a stream raster comes back as hundreds of thousands
of cell-boundary rings: measured 2026-09-03 on one Gemeinde, **535,903 features**
where the channels number in the hundreds.

Then clip to the boundary from H1 and `render_map` the result. State the threshold
you used in the report rather than eyeballing it.

**Never tune the hydrological threshold to satisfy the renderer.** If `render_map`
refuses the layer as too large, that is a display limit, not a finding about the
terrain — vectorise properly (above) or show a subset. A threshold raised from 100
to 10,000 until the map loads answers a different question than the one asked.

**Open the map before reporting it.** A wrong threshold and the sign trap above both
produce a valid, empty file. `ok: true` means a file was written, not that anything
is on it.

### Report it as a prediction
This is a topographic model, not an observation and not a flood map. Name what it
does not contain: the sewer network, infiltration, and any rainfall amount. Results
without those caveats claim more than the data supports.

## Notes
- `render_map` handles rasters too: a single-band raster (DEM, slope, accumulation)
  is drawn as a colourised image overlay, and vector layers stack on top of it.
- Slope in degrees assumes matching horizontal/vertical units; if the DTM is in
  degrees (geographic), reproject first or the slope values will be wrong.

## Report
List the products created with their file paths, the DTM's CRS, and the slope value
range from the validation step.
