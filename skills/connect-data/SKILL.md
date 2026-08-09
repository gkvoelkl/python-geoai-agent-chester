---
name: connect-data
description: Onboard a user-provided data source (GeoPackage, SpatiaLite, or PostGIS) — list its layers/tables and bring it in, either referenced in place or as a working copy. Use when the user points Chester at their own data ("here is my house.gpkg", a .sqlite file, or a PostGIS connection).
version: 1
---

# Connect a data source

The user has their own data — a GeoPackage (`.gpkg`), a SpatiaLite database
(`.sqlite`, a.k.a. GeoSQLite), or a PostGIS connection — and wants Chester to work
with it. All three are **container connectors**: they hold **one or more
GeoDatasets** (layers / tables), so the flow is the same for each.

**The user's data is precious — it is never a disposable cache entry.** The
original always stays `source: user` and is **never pruned**. Only copies pulled
into the cache age.

## Inputs (ask the user for any that are missing)
- the source: a file path (`.gpkg` / `.sqlite`) or a PostGIS DSN + schema
- whether to **reference in place** or **import a working copy** (step 2)
- which layer(s)/table(s) to work with (after you have listed them)

## Steps

1. **Recognise the source.** Resolve the file path with `resolve_path` (or take
   the PostGIS DSN) and confirm it is a container — a GeoPackage/SpatiaLite file
   or a reachable PostGIS schema. If the path does not exist or the DB is
   unreachable, say so and stop.

2. **Ask how to bring it in** — this decides lifecycle, so ask before pulling
   anything:
   - **In place** (add as a read-only **data root**): zero-copy, the original is
     read directly, **never pruned**, always current. Prefer this for large or
     authoritative data, or when the user does not want anything duplicated.
   - **Working copy** (import into the cache with `geodataset_fetch`): a copy or
     subset that **ages like any download**; the original is untouched and can be
     re-imported. Prefer this for an isolated working set.

   If the user has no preference, default to **in place** — it never risks their
   data and is always fresh.

3. **List the datasets.** `geodatasets_list(connector=…)` — a container holds one
   *or many* layers. Report each with its CRS/SRID, geometry type and feature/row
   count. For a single-layer GeoPackage this is one dataset; for a rich one it may
   be several (`buildings`, `parcels`, `roof_areas`, …).

4. **Describe on request.** If the user needs detail before choosing,
   `geodataset_describe(connector, dataset)` gives columns, types and extent.

5. **Bring in the chosen datasets.**
   - *In place:* add the file/folder to the `geodata.roots` config (or note it as a
     data root); `geocache_sync` then catalogues each layer.
   - *Working copy:* `geodataset_fetch(connector, dataset, bbox?, where?, output)`
     subsets the layer into a cache GeoPackage. Keep any `bbox`/`where` tight — pull
     a *window of interest*, not the whole table.

6. **Catalogue & confirm.** Run `geocache_sync()` so every layer appears in
   `geocache.md` (one line per dataset). Confirm what is now available.

7. **Report.** Tell the user which datasets are connected, how (in place vs working
   copy), and their CRS/geometry/count — e.g. *"house.gpkg holds 3 layers:
   buildings (EPSG:25832, 412 polygons), parcels (…), roof_areas (…); referenced in
   place."* From here every QGIS tool can use them; derived outputs land in the
   cache and age normally.

## PostGIS / SpatiaLite — safety (non-negotiable)
The `where`/`bbox` you pass are model-generated, so for the SQL-backed connectors:
- **bind parameters** — never string-concatenate SQL;
- **whitelist** the `dataset`/`table` to the names returned by `geodatasets_list()`;
- use a parameterised `ST_MakeEnvelope(..., srid)` for the bbox;
- rely on the connector's **read-only role** — Chester only pulls *from* the DB.

## Notes
- A multi-layer container expands to several inventory lines — list and confirm
  the layers; do not assume one file = one dataset.
- Never copy the user's master into `geocache/` and leave it on the default TTL —
  that is the working-copy path only, and the master should stay a data root.
- Reprojection/analysis happens on the cache copies or in place via the normal
  QGIS tools; this skill only handles getting the data *connected*.
