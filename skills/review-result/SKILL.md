---
name: review-result
description: Visually validate a geo result before finalising — render a static snapshot with inspect_map, LOOK at it, and catch errors the numbers miss (wrong CRS / off-coast placement, wrong admin level, gaps/overlaps in a partition, a flat/broken choropleth, NDWI/NDVI bleeding onto cloud), then redo the offending step. Needs a vision-capable model.
version: 1
---

# Review the result (visual validation)

Correctness is a loop phase, and a *second channel* beats one. `check_crs` and
`sanity_check_result` catch numeric problems; a **look at the rendered map** catches
what numbers can't — a layer in the ocean from a CRS bug, a "district" that is
really the whole metro, a choropleth that is all one colour from a broken join.
This skill runs that visual check before you finalise a non-trivial result.

## When to use
- After producing a result worth showing (a selection, overlay, choropleth,
  classified raster, terrain product) and **before** reporting it as done.
- Especially when a CRS reprojection, a spatial join, or a classification was
  involved — the error classes a picture reveals.

## Seeing the image
Assume you can see the snapshot `inspect_map` returns. If you **cannot** (you would
say "I see no image"), you are a text-only model — call
`inspect_map(..., via_vision_model=True)` and the configured fallback vision model
(`model.vision_model`) looks at it for you and returns a written verdict. If no
fallback is configured either, say so and rely on `check_crs` /
`sanity_check_result` — do not claim a visual check you could not run.

## Steps

1. **Gather the layers to look at.** The result layer(s), plus one context layer if
   it helps orient (the study-area boundary, the source points behind a choropleth).

2. **Render a snapshot.** `inspect_map(layers=[...], column=<the choropleth field,
   if any>, question="<what to check>")`. It returns the image plus a per-layer
   summary (features, geometry, CRS, WGS84 extent, value range).

3. **Look, using the checklist.** Judge the returned image against the task:
   - **Placement** — is the data where the place actually is? Off-coast / wrong
     hemisphere ⇒ a CRS or lon/lat-swap bug.
   - **Extent/scale** — does the footprint match the expected area? A shape filling
     the frame ⇒ wrong admin level or the wrong layer.
   - **Coverage/tiling** — do partition layers (Voronoi catchments, districts) tile
     without gaps or overlaps?
   - **Choropleth** — does the colour actually vary? Uniform ⇒ a broken join or a
     constant/null field. Cross-check the summary's `value_range`.
   - **Index bleed** — for NDWI/NDVI, does water/vegetation follow real features and
     not cloud/shadow?
   - Cross-check against the numeric summary and the task's own plausibility
     (a flood is visibly larger than the normal channel).

4. **If it contradicts the task, fix the cause — do not report the result.** Map the
   symptom to the step and redo it: reproject (`qgis_reproject`), re-join
   (`native:joinattributesbylocation`), pick the right layer, re-tag the OSM query,
   adjust the index threshold. Then `inspect_map` **again**. Cap at ~2 fix→re-check
   rounds; if still wrong, report the problem honestly rather than looping.

5. **When plausible, finalise.** Produce the user-facing map with `render_map`
   (interactive HTML) and report — now with visual confirmation behind it.

## Notes
- **Advisory, not a gate.** A vision judgement is fallible; use it to catch obvious
  wrongness, not to block forever. Two rounds, then decide.
- **Snapshot ≠ deliverable.** `inspect_map`'s flat PNG is for *you* to inspect;
  `render_map`'s interactive HTML is what the user gets.
- **Cost.** Review the **final** result (or on request), not every intermediate —
  each review is an extra render plus a look.

## Report
State that a visual check was done and what it confirmed — or what it caught and how
you fixed it (e.g. "the buildings first rendered offshore → the source was EPSG:4326
mislabelled; reprojected and re-checked"). Then the result + its map.
