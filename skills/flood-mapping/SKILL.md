---
name: flood-mapping
description: Map open water / flooded areas from Sentinel-2 imagery for a place and time window using STAC search and the NDWI water index. Earth observation workflow.
version: 1
---

# Flood / water mapping

Map open water for an area and date range from Sentinel-2 satellite imagery: find a
suitable scene via STAC, fetch the needed bands for the area of interest, classify
water with NDWI, and present the result.

## Inputs (ask the user for any that are missing)
- `area` — place name or bounding box [west, south, east, north]
- `date_range` — ISO range, e.g. "2021-07-01/2021-07-31"
- `max_cloud` — max cloud cover percent (default 20)
- a working directory for outputs (default the workspace)

## Steps
1. **Locate.** If `area` is a place name, `geocode(area)` to get its bbox. Keep the
   area of interest small — a town, not a whole county — so band downloads stay light.
2. **Find a scene.** `stac_search(bbox, datetime=date_range, max_cloud=<max_cloud>)`.
   Pick an item; prefer low `cloud_cover`. Note its `assets` (band URLs).
   The collection is Sentinel-2 L2A; the bands you need are `green` and `nir`.
3. **Fetch bands for the AOI.** For each of green and nir:
   `fetch_raster(url=<asset href>, bbox=<aoi>, output_path=".../<band>.tif")`. This
   downloads only the AOI window, not the whole scene. If the result says the bbox
   does not overlap, the chosen tile does not cover the AOI — pick another item
   (tiles from a neighbouring UTM zone often appear in the same search).
4. **Detect water.** `detect_water(green=".../green.tif", nir=".../nir.tif",
   mask_path=".../water_mask.tif", threshold=0.0, polygons_path=".../water.geojson")`.
   NDWI > threshold is water; raise the threshold if too much is flagged, lower it if
   water is missed.
5. **Validate.** `sanity_check_result(".../water.geojson")` — confirm a non-empty,
   valid result. Sanity-check the water fraction against expectation (a flood shows a
   much larger water area than the normal river channel).
6. **Map.** `render_map(layers=[".../water.geojson"], output_path=".../flood_map.html")`.

## Notes
- This uses the NDWI spectral index (fast, local, no GPU). It maps *open water*;
  it does not distinguish flood water from permanent water bodies on its own —
  compare against a pre-event scene if the user needs flood extent specifically.
- Clouds and cloud shadows can be misclassified; prefer low-cloud scenes and a
  sensible threshold.

## Report
State the scene id and date, its cloud cover, the AOI, the detected water area /
fraction, and the output files (water polygons + map). Note the NDWI threshold used.
