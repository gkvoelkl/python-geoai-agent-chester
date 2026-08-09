---
name: find-official-data
description: Find and fetch authoritative open data when a layer is NOT in OpenStreetMap — administrative/city-district (Stadtbezirk) boundaries, official thematic layers — via open-data catalogs (CKAN), OGC WFS, or a web-search fallback, then use it (e.g. clip/count features within a district). Data acquisition workflow for the OSM gaps.
version: 1
---

# Find official data (when OSM falls short)

OpenStreetMap is the default vector source, but it is volunteered and patchy.
Many everyday layers are simply **not in OSM** — most sharply **sub-municipal
administrative boundaries** (city districts / *Stadtbezirke*). This skill is the
fallback: recognise the OSM gap, find the layer in an **official open-data
source**, fetch it, and use it.

Worked example throughout: *"How many bus stops are in the Innenstadt district of
Regensburg?"* — OSM has the bus stops but **not** the Innenstadt boundary.

## When to use
- OSM returned nothing / nothing relevant for a needed layer, **or**
- the task inherently needs authoritative data (admin boundaries, official
  statistics geometries, cadastral/land-use layers a municipality publishes).

## Inputs (ask for any that are missing)
- `area` — place / region (e.g. "Regensburg")
- `layer_needed` — what is missing (e.g. "Stadtbezirk Innenstadt boundary")
- what to do with it (e.g. "count OSM bus stops within it")

## Steps

1. **Confirm the OSM gap.** If you tried OSM (`osm_features` / `osm_query_raw`)
   and the feature is absent, say so — do not silently fall back to a coarser
   layer (e.g. the whole-city boundary) and pretend it answers the question.

2. **Search open-data catalogs.** `geodata_search("<layer> <area>")`, e.g.
   `geodata_search("Stadtbezirke Regensburg")`. It defaults to the EU aggregator
   *data.europa.eu*; pass `catalog_url="govdata.de"` (or another portal) if the
   default misses. Inspect `candidates` — geospatial ones are ranked first. Pick
   the one whose `title`/`publisher` matches; note its `license` (a **null**
   license means terms are unverified — flag that in the report).

   > **Delegate a deep hunt.** When the right source isn't obvious and finding it
   > would take several web searches, hand that off:
   > `delegate_task(agent="data-scout", task="<place> — <layer needed>")`. The
   > `data-scout` sub-agent runs the search in its own context and returns a short
   > ranked list of working URLs (service type / format / CRS / licence); you then
   > fetch the best one. Keeps the main context clean instead of searching inline.

3. **Fetch the layer.**
   - **WFS resource** (has `wfs_url` + `typename`): if unsure which feature type,
     `wfs_capabilities(wfs_url)` lists them. Then
     `wfs_features(wfs_url, typename, ".../districts.gpkg")`.
   - **Direct file** (GeoJSON / zipped Shapefile / GeoPackage / GML):
     `fetch_vector(url, ".../districts.gpkg")`.

4. **Escalate the administrative level (before giving up).** If neither OSM nor the
   catalog has the *specific* unit, it is very likely part of a comprehensive
   dataset for a **containing** level held by a more central authority — all
   Gemeinden of the Kreis / Land / Bund. Region keys (AGS / NUTS) encode this as a
   prefix, so `region_hierarchy("<AGS or NUTS>")` returns the wider scopes to try
   (e.g. Gemeinde "09375117" → Kreis "09375" → Land "09" → Bund ""). Then:
   - *Boundaries / geometry:* fetch the comprehensive higher-level set and **clip**
     to your area — a Land-wide WFS (a Gemeinden/`verwaltungsgebiete` typename on
     the state geodata service) or the federal **BKG VG250** (all German admin
     units, open vector), then `qgis_clip` / `qgis_extract_by_attribute` to the unit.
   - *Statistics:* `stats_table("wikidata", "<wider prefix>")` (e.g. "09" = every
     Bavarian Gemeinde), filtered on AGS.
   **Escalate the scope, keep the granularity:** fetch a set that still holds the
   per-unit values; never substitute a higher-level aggregate for a missing unit.

5. **Web-search fallback (only if step 2 + escalation found nothing usable).**
   `web_search` for the municipality's open-data / geoportal (e.g. "Regensburg
   Stadtbezirke WFS GeoJSON opendata"); `web_fetch` the page to extract a service
   or file URL, then hand it to `wfs_capabilities` / `wfs_features` / `fetch_vector`
   as above. This path is brittle — prefer the catalog / a higher-level dataset. If
   nothing authoritative turns up, report that the layer could not be sourced
   rather than approximating.

6. **Isolate the specific feature** if the layer holds many. Inspect columns with
   `vector_info(".../districts.gpkg")`, then select by name — e.g.
   `qgis_extract_by_attribute(".../districts.gpkg", field="Name",
   value="Innenstadt", output_path=".../innenstadt.gpkg")`. Colon/space-safe, no
   expression quoting.

7. **Bring both layers to one metric CRS.** Official German data is often
   EPSG:25832; OSM is EPSG:4326. `check_crs` each, then `qgis_reproject` so the
   target layer and the district share **one** metric CRS before any spatial
   test. Reproject the OSM layer to match the district (or both to 25832).

8. **Do the spatial operation.**
   - *Count/select within:* `qgis_extract_by_location(input_path=".../bus_stops.gpkg",
     reference_path=".../innenstadt.gpkg", output_path=".../stops_in_district.gpkg",
     predicate="within")` — a **spatial** predicate, never an attribute filter
     (the points carry no district name). The output feature count is the answer.
   - *Cut to the area:* `qgis_clip(input, overlay=district, output)`.

9. **Validate.** `sanity_check_result(".../stops_in_district.gpkg")` — non-empty,
   valid. Is the count plausible (fewer than the whole-area total)?

10. **Map.** `render_map(layers=[".../innenstadt.gpkg", ".../stops_in_district.gpkg"],
   output_path=".../result.html")` — show the district and the contained points.

## Notes
- **Trust the URL, not the label.** `geodata_search` already classifies resources
  by real service type (a WFS is often mis-tagged "CSV" in the catalog); use the
  `service`/`wfs_url`/`typename` it returns, not the raw format string.
- **Predicate discipline.** "within a district" is a `within`/`intersects`
  spatial test between geometries — not a `where`/attribute filter. Points that
  fall in a district almost never carry the district's name as a tag.
- **Coverage is not guaranteed.** A municipal layer may live only on the city's
  own portal, not in a federated catalog — that is what the web-search fallback is
  for; and some layers are genuinely unpublished.
- **Escalate up the hierarchy, not the answer.** Missing per-unit data is usually
  found in the containing level's comprehensive dataset (`region_hierarchy` gives
  the AGS/NUTS prefix chain) — but only if that dataset still carries the per-unit
  granularity. Fetching the Kreis *total* and reporting it as the Gemeinde value is
  fabrication; if no level has the granularity you need, say so.

## Report
State: that the layer was absent from OSM; the **source** it came from (catalog,
publisher, service) and its **license** (flag if unknown); the CRS both layers
shared for the test; the spatial predicate used; and the result (e.g. the count of
contained features) with the output files (isolated feature, selection, map).
