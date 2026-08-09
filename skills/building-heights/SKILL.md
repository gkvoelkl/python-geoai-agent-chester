---
name: building-heights
description: Get true building heights and analyse them (tallest buildings, height distribution, affected streets). Primary source is open LoD2 building models (laser-measured height per building); DSM−DTM only as a fallback when the user brings their own high-resolution rasters.
version: 2
---

# Building heights

Analyse how tall buildings are — tallest buildings over a threshold, height
distribution/inequality, the streets affected. **Use real, per-building heights.**

## Where the height comes from (in order of preference)

1. **Open LoD2 building models — the authoritative source (use this first).**
   The German Bundesländer publish LoD2 3D building models as open data; every
   building carries a **laser-measured height** (`bldg:measuredHeight`, ALS-derived,
   mm precision). This is the true per-building height — better than any raster
   differencing (no vegetation, no edge artefacts, already summarised per building).
   - `lod2_sources()` — which Bundesländer are **wired** (fetchable now: Bayern
     `BY`, Nordrhein-Westfalen `NW`) vs. **documented** (open, but access not yet
     wired — tell the user the portal from the list).
   - `fetch_lod2(bbox, output_path, state?, street?)` — `bbox` = [west, south,
     east, north] in WGS84 (from `geocode`). The Bundesland auto-detects from the
     bbox; pass `state` (e.g. "BY") to force it; pass `street` to keep only that
     street. Output is a GeoPackage with a **`measured_height`** column (metres) in
     a metric CRS (EPSG:25832/25833) — no reprojection needed before stats.

2. **DSM − DTM (only if the user supplies a high-resolution DSM).**
   Building height = DSM − DTM, summarised per footprint. The **DTM** half is now
   fetchable at 1 m via `fetch_dgm1(bbox, ...)` (Bayern/NRW, EPSG:25832), so this
   route needs only a real high-resolution **DSM** from the user (there is no open
   DSM). Do **not** substitute `fetch_dem`: Copernicus GLO-30 is a single ~30 m
   surface model — far too coarse to resolve a building — so DSM − DTM would be
   meaningless. Where LoD2 exists it already gives the measured height, so prefer
   route 1; use DSM − DTM only outside LoD2 coverage with a user DSM. If neither a
   LoD2 source nor a user DSM exists, say so rather than faking a height.

3. **`building:levels` × storey height (last-resort estimate).** Only if neither of
   the above is available. Most OSM buildings lack the tag, so the sample is tiny
   and biased — flag it clearly as a rough estimate, never as measured.

## Steps — LoD2 route (default)
1. **Locate.** `geocode(place)` → a bbox. For a single street, keep the bbox tight
   around it.
2. **Fetch.** `fetch_lod2(bbox, output_path=".../buildings.gpkg", street="…")`.
   Check the returned `state`, `buildings`, `with_height` and `height_stats_m` are
   plausible. If it reports the state is only *documented*, tell the user the open
   portal and stop (no faking).
3. **Analyse** on the `measured_height` column (already in metres):
   - Tallest over a threshold: `vector_filter(expression="measured_height > <t>")`.
   - Distribution / inequality (e.g. a Gini coefficient): `qgis_python` over the
     `measured_height` values.
   - Average/median height: read the tool's `height_stats_m`, or `qgis_field_sum`.
4. **Affected streets** (optional): the layer already carries `street`; group by it,
   or buffer tall buildings and select roads that intersect.
5. **Validate.** `sanity_check_result(".../buildings.gpkg", expected_geometry=
   "MultiPolygon")` — non-empty, valid, heights in a sane range (a house ≈ 5–30 m).
6. **Map.** `render_map(layers=[".../buildings.gpkg"], column="measured_height",
   output_path=".../building_heights.html")` — choropleth by height.

## Steps — DSM−DTM fallback (user rasters only)
1. `check_crs` on the footprints; reproject to a metric CRS if geographic.
2. `qgis_raster_calc(input_a=dsm, input_b=dtm, formula="A-B", output_path=
   ".../height.tif")`.
3. `qgis_zonal_stats(zones_path=buildings, raster_path=".../height.tif",
   statistics=["max"], prefix="h_", output_path=".../buildings_h.geojson")`.
4. `vector_filter(expression="h_max > <threshold>", ...)`, then validate and map as
   above.

## Report
State the source and licence (LoD2 returns a `licence` — cite it), the number of
buildings, the height figures (min/max/mean/median or the threshold count), the
streets involved, and the output files. If you fell back to `building:levels`, say
so explicitly and call it an estimate.
