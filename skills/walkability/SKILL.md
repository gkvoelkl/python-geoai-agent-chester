---
name: walkability
description: Travel-time accessibility on the street network — walk/bike isochrones ("what's reachable in 15 minutes on foot"), amenity catchments, and 15-minute-city coverage. Uses real network reach, not straight-line buffers.
version: 1
---

# Walkability / travel-time accessibility

Answer "how far can you get, and what can you reach, in N minutes on foot (or by
bike)" using the **street network** — real routed reach, not a straight-line
buffer. The core tool is `qgis_service_area`, which returns an **isochrone
polygon** (the area reachable within a time budget along the network).

Two common shapes of question:
- **From a place:** what is within a 15-minute walk of an address / point (and how
  many amenities fall inside).
- **15-minute city / coverage:** which parts of an area are within a N-minute walk
  of a given amenity type (supermarkets, schools, …), and what share of the
  area/population that covers.

## Inputs (ask the user for any that are missing)
- the **origin(s)** — an address/place, a point, or an amenity type whose
  catchments you want.
- the **time budget** in minutes (default 15) and **mode** (walk / bike / drive;
  default walk).
- the study area (city/bbox) for fetching the network and destinations.
- a working directory for outputs (default the workspace).

## Steps
1. **Fetch the street network.** `osm_features(tags={"highway": true}, bbox=…,
   output_path=".../roads.gpkg")` for the study area (use `geocode(place)` for the
   bbox). Keep the line features (the network); the wrapper routes along them.
2. **Reproject the network to a metric CRS.** `qgis_reproject(roads,
   target_crs="EPSG:25832", output_path=".../roads_25832.gpkg")` — OSM arrives in
   EPSG:4326 (degrees), and `qgis_service_area` refuses a degree network (a travel
   distance must be in metres). Pick the local metric CRS (EPSG:25832 for Germany,
   or the local UTM zone).
3. **Build the isochrone(s).** For each origin:
   `qgis_service_area(network_path=".../roads_25832.gpkg", start_lon=<lon>,
   start_lat=<lat>, minutes=15, output_path=".../iso.gpkg", mode="walk")`.
   - `start_lon`/`start_lat` are the origin in WGS84 (lon, lat) — pass a `geocode`
     centroid straight through, in order: it is `[lon, lat]`, so
     `start_lon = centroid[0]`, `start_lat = centroid[1]`. They are named so the
     order can't be swapped; QGIS transforms the point to the network CRS. Example:
     geocode the address → `start_lon=12.097, start_lat=49.013`. (To pass a point
     already in the network CRS instead, set `start_crs` to that CRS.)
   - `mode` sets the assumed speed (walk 4.5, bike 15, drive 50 km/h); the result
     reports `reach_distance_m` (the network distance the budget buys).
4. **Analyse what's reachable** (if the question asks it):
   - Count destinations inside an isochrone: fetch the amenity points
     (`osm_features(tags={"shop": "supermarket"}, …)`), reproject to the same CRS,
     then `qgis_run("native:countpointsinpolygon", {"POLYGONS": iso,
     "POINTS": amenities, "FIELD": "n", "OUTPUT": …})`, or
     `qgis_extract_by_location(amenities, iso, predicate="within")`.
   - **Coverage / 15-minute city:** build an isochrone per amenity (loop step 3
     over each point), merge them (`native:union` or `qgis_dissolve`), then compare
     the covered area to the city polygon (`qgis_field_sum(covered, "$area")` vs the
     city area) for a "% within a 15-min walk" figure. For population coverage,
     intersect the covered area with a population choropleth (`stats_table`
     "wikidata" per Gemeinde) and sum.
5. **Validate.** `sanity_check_result(".../iso.gpkg")` — the isochrone is a
   non-empty polygon in a metric CRS. Sanity-check the magnitude: a 15-min walk
   isochrone is on the order of ~1 km reach, not tens of km. If the polygon is
   empty or the tool reports few reachable nodes, the `start` is probably off the
   network (wrong CRS or not near a road).
6. **Map it.** `render_map(layer=".../iso.gpkg", …)` with the origin and any
   reachable destinations overlaid; for a coverage map, symbolise covered vs
   uncovered.

## Notes
- **Network reach ≠ buffer.** Do not substitute `qgis_buffer` for the isochrone —
  a buffer ignores the street layout and overstates reach. `qgis_service_area` is
  the point of this skill.
- **CRS discipline.** Both the network and any destination layer must share the
  metric CRS before counting/measuring; areas and distances in degrees are wrong.
- **Speeds are assumptions.** The walk/bike/drive speeds are defaults; state them
  in the answer so the result is reproducible.
- Never fabricate reach or coverage numbers — if the network or destinations can't
  be fetched, report the blocker.
