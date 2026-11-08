"""Source handbooks for chester-code: what each data source is for, and a template program.

After LLM-Find (Ning, Li et al. 2024, arXiv 2407.21024): before coding, the coder picks a
source for each data layer from a short **index**, and the handbook of the chosen source
goes into its context — what the source is for and what not, the functions that fetch
it, its known traps, and a **template program** to adapt. Measured there: templates
raise the success rate markedly. Measured here (2026-10-24, `mean-elevation-per-district`):
without them the coder searched for the city districts inside its programs for 18 runs
and lost the goal.

The notes carry the traps the wrapper modules' instructions teach the single-step agent
(`module_instructions`), rewritten as facts about the data — without their "call X, then
Y" phrasing, which the program replaces. What holds for every task whatever its source
(a travel time is not a buffer, a selection is drawn as a selection, …) is
:data:`ANALYSIS_NOTES`, part of every run's instructions.

Each template is checked by a Test-Level-1 test against the real tool signatures, the
same way as the example in the coder's instructions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Handbook:
    """One data source family: its index line, its functions, its traps, its template."""

    id: str
    summary: str
    functions: tuple[str, ...]
    notes: str
    template: str


#: Functions every program may call whatever sources were chosen — the core of most chains.
ALWAYS = frozenset({"geocode", "osm_features", "fetch_boundaries",
                    "vector_split_by_geometry", "vector_merge"})

HANDBOOKS: tuple[Handbook, ...] = (
    Handbook(
        id="osm",
        summary="OpenStreetMap features: POIs (schools, shops), roads, buildings, land use, "
                "water, parks — worldwide, by place name or bbox.",
        functions=("osm_features", "vector_split_by_geometry", "vector_merge"),
        notes="""\
- Pass `place="<City>"`: the result is clipped to the official boundary. A bbox is only a
  window and needs clipping later.
- Several tag keys are OR: `{"amenity": "school", "building": true}` is schools OR buildings.
  `True` means "any value of this key"; the string `"true"` matches nothing.
- A result often mixes points and polygons (`mixed_geometry`): split it, keep the families
  you need, merge them back before computing.
- Output is EPSG:4326 (degrees): reproject to a metric CRS before any distance or area.
- `where={"addr:street": "Hollerweg"}` filters during the download — no filter step after.
- Buildings are `{"building": True}`, the street network `{"highway": True}`.
- A Landkreis around a kreisfreie Stadt has a hole there: `place=` and `vector_clip`
  honour it, a bbox does not — "buildings in Landkreis Regensburg" excludes the city.""",
        template='''\
pois = await osm_features(place="Regensburg", tags={"amenity": "school"},
                          output_path="schools.gpkg")
assert pois.get("ok"), pois
parts = await vector_split_by_geometry(path=pois["output"], output_prefix="schools")
assert parts.get("ok"), parts
keep = [p["output"] for p in parts["parts"]
        if p["geometry_type"] in ("Point", "Polygon", "MultiPolygon")]
merged = await vector_merge(input_paths=keep, output_path="schools_all.gpkg")
assert merged.get("ok"), merged''',
    ),
    Handbook(
        id="boundaries",
        summary="Official administrative boundaries: Germany (BKG: state, district, "
                "municipality), Austria, Switzerland. Not below the municipality.",
        functions=("fetch_boundaries", "fetch_austria_boundaries", "fetch_swiss_boundaries",
                   "boundaries_levels"),
        notes="""\
- `fetch_boundaries(match="Regensburg")` finds the smallest unit with that name (the city,
  not the district of the same name); pass `level="KRS"` for the Landkreis.
- Output is EPSG:25832 and carries the AGS key for joining statistics.
- `match` is a key prefix ("09" = Bayern, "09162" = München) or a name. Levels: `STA`, `LAN`,
  `RBZ`, `KRS`, `VWG`, `GEM` (keyed by AGS), `NUTS1`–`NUTS3` (keyed by NUTS_CODE).
- City districts (Stadtbezirke, Stadtteile) are NOT here — use the `city_districts` source.
- Switzerland (EPSG:2056): all units of a canton are `canton="Bern"`, never `match` — the
  `bfs_nummer` is not hierarchical. The GEMEINDE layer already carries `einwohnerzahl`.
- Austria (EPSG:31287): the key `g_id` is hierarchical like the AGS ("7" = Tirol).
- A polygon whose `name` is not the area asked for is a different area — check it.""",
        template='''\
city = await fetch_boundaries(output_path="regensburg.gpkg", match="Regensburg")
assert city.get("ok"), city
clipped = await vector_clip(input_path="result.gpkg", overlay_path=city["output"],
                            output_path="result_city.gpkg")
assert clipped.get("ok"), clipped''',
    ),
    Handbook(
        id="city_districts",
        summary="Sub-municipal units — Stadtbezirke, Stadtteile, statistical districts — "
                "from the city's own open data (WFS) found via the open-data catalog.",
        functions=("geodata_search", "wfs_capabilities", "wfs_features", "fetch_vector"),
        notes="""\
- These are official city data, usually a WFS: search the catalog, take a WFS resource,
  pass its `wfs_url` and `typename` to `wfs_features`. Copy URL and typename exactly from
  the search result — never type them yourself.
- Fall back to OSM only if the catalog has nothing: `osm_features(place=...,
  tags={"boundary": "administrative"}, where={"admin_level": "9"})` (try "10" as well).
- OSM and the geocoder answer a district name with something that only sounds right — a
  world-heritage outline for "Altstadt", a point for "Zentrum". Read the features' names
  (`vector_info(path=..., values_of="name")`) and keep the ones asked for; if nothing
  official exists, say so in the answer.""",
        template='''\
found = await geodata_search(query="Stadtbezirke Regensburg")
assert found.get("ok"), found
wfs = [r for c in found["candidates"] for r in c["resources"] if r.get("service") == "WFS"]
assert wfs, found
districts = await wfs_features(url=wfs[0]["wfs_url"], typename=wfs[0]["typename"],
                               output_path="districts.gpkg")
assert districts.get("ok"), districts''',
    ),
    Handbook(
        id="terrain",
        summary="Elevation models: Copernicus DEM 30 m (worldwide), open DGM1 1 m "
                "(Bayern, NRW, Brandenburg, MV), Austria 10 m, swissALTI3D (Switzerland).",
        functions=("fetch_dem", "fetch_dgm1", "fetch_austria_dem", "fetch_swissalti3d"),
        notes="""\
- In Bayern, NRW, Brandenburg and Mecklenburg-Vorpommern take `fetch_dgm1` first: official,
  1 m, metric. It refuses more than 200 tiles of 1 km² — about 14 × 14 km, enough for a
  city like Regensburg (~160 tiles). Only outside those states, or for a larger area, fall
  back to `fetch_dem` (Copernicus, 30 m). Never claim DGM1 is unavailable in Bayern.
- Take the bbox from `geocode(...)["bbox"]`.
- `zonal_stats` reprojects the zones to the raster's CRS itself — never warp the DEM.
- `fetch_dem` is EPSG:4326 (degrees): for `slope`, `aspect` or areas the DEM must be
  metric. DGM1 is metric (25832/25833), swissALTI3D EPSG:2056, the Austrian DGM
  EPSG:3035 with nodata −9999.
- Each zone reports its `coverage`; a zone the raster does not reach is `null`, never 0.
- A hillshade is a picture — never read heights or slopes off it.
- `fill_sinks` before `flow_accumulation` (both need GRASS), or the drainage stops at the
  first depression.
- Building heights are `buildings3d`, never a DEM: 30 m cannot resolve a building.""",
        template='''\
place = await geocode(query="Regensburg")
assert place.get("ok"), place
dgm1 = await fetch_dgm1(bbox=place["bbox"], output_path="dgm1.tif")
if dgm1.get("ok"):
    dem_path = dgm1["output"]
else:  # outside the four states, or more than 200 tiles: Copernicus 30 m
    dem = await fetch_dem(bbox=place["bbox"], output_path="dem.tif")
    assert dem.get("ok"), dem
    dem_path = dem["output"]
stats = await zonal_stats(raster_path=dem_path, zones_path="districts.gpkg",
                          output_path="districts_elevation.gpkg", stat="mean")
assert stats.get("ok"), stats''',
    ),
    Handbook(
        id="aerial",
        summary="Official aerial imagery (DOP, 10–20 cm) of the German states; RGB+NIR "
                "outside Bayern, RGB only in Bayern.",
        functions=("fetch_dop", "spectral_index", "detect_water"),
        notes="""\
- Check `has_nir` in the result before any NIR index (NDVI, NDWI): Bayern delivers RGB only.
- Imagery is large (tiles of 18–83 MB): keep the bbox to a few km.
- Outside the wired states only a WMS picture exists (`fetch_wms_map`): WMS pixels are
  colours, never data — show them, never compute on them.""",
        template='''\
place = await geocode(query="Tegernheim")
assert place.get("ok"), place
dop = await fetch_dop(bbox=place["bbox"], output_path="dop.tif")
assert dop.get("ok"), dop''',
    ),
    Handbook(
        id="satellite",
        summary="Satellite scenes (Sentinel-2, Landsat, …) from STAC catalogs, by bbox, "
                "date range and cloud cover.",
        functions=("stac_search", "stac_catalogs", "fetch_raster"),
        notes="""\
- `stac_search` lists scenes and asset URLs; `fetch_raster` cuts one asset to the bbox.
- Pass `max_cloud` for optical scenes.
- `stac_search` downloads no pixels. Catalogs: "earth-search" (default),
  "planetary-computer" (Landsat, Sentinel-1, WorldCover), "cdse".""",
        template='''\
place = await geocode(query="Regensburg")
assert place.get("ok"), place
scenes = await stac_search(bbox=place["bbox"], datetime="2024-06-01/2024-08-31",
                           collections=["sentinel-2-l2a"], max_cloud=20)
assert scenes.get("ok"), scenes''',
    ),
    Handbook(
        id="statistics",
        summary="Statistics by region: Eurostat, Wikidata (population per municipality, "
                "AGS keys), World Bank.",
        functions=("stats_search", "stats_table", "region_hierarchy", "stats_sources"),
        notes="""\
- Search first (`stats_search`), then fetch the table by its `code`.
- Join on the AGS key with official boundaries (`fetch_boundaries` carries it), at the
  level of the table: Gemeinde figures → `GEM`, Kreis → `KRS`, Eurostat NUTS-3 → `NUTS3`.
- Sources: `wikidata` (Germany, per Gemeinde: `ags`, `population`, `area_km2`; the code of
  "Landkreis Regensburg" is "09375"), `eurostat` (NUTS key `geo`), `worldbank` (`iso3`).
- Check `vector_join`'s `unjoined` before mapping: a key read as a number has lost its
  leading zero, and a choropleth over an empty column still draws.
- No value for a unit: widen the scope (`region_hierarchy(code)`, e.g. "09" = all of
  Bayern), keep the granularity. Never pass off a higher-level aggregate as the unit's
  value.""",
        template='''\
hits = await stats_search(source="wikidata", term="Landkreis Regensburg")
assert hits.get("ok"), hits''',
    ),
    Handbook(
        id="transit",
        summary="Public transport timetables (GTFS): stops and routes with trip counts and "
                "headways, Germany, Switzerland, Austria.",
        functions=("gtfs_feeds", "fetch_gtfs_stops", "fetch_gtfs_routes"),
        notes="""\
- Feeds: `de_nv` (local bus/tram, national), `de_rv` (regional rail), `de_fv`, `de_full`;
  `ch_rail`, `ch_bus`, `ch_full`; Austria is gated (a local zip path works as `feed`).
  Always pass a bbox for the national `de_nv`/`de_full`.
- Stops and routes are EPSG:4326: reproject before any distance.
- The stop label is `stop_name`. `mean_headway` is NaN for a stop with one trip — "not
  frequently served", never 0.
- Swiss `ch_*` routes are per journey, not per line: use stops for CH service quality.""",
        template='''\
place = await geocode(query="Regensburg")
assert place.get("ok"), place
stops = await fetch_gtfs_stops(feed="de_nv", output_path="stops.gpkg",
                               bbox=place["bbox"])
assert stops.get("ok"), stops''',
    ),
    Handbook(
        id="buildings3d",
        summary="Official LoD2 building models with heights (German states), and 3D city "
                "models (CityJSON, swissBUILDINGS3D, Vienna).",
        functions=("lod2_sources", "fetch_lod2", "fetch_cityjson", "cityjson_to_geopackage",
                   "fetch_swissbuildings3d", "fetch_vienna_buildings"),
        notes="""\
- `fetch_lod2` returns footprints with a `measured_height` column (metres) in a metric
  CRS — no reprojection before statistics; keep the bbox small (a quarter).
- Wired states: Bayern, NRW, Brandenburg, Mecklenburg-Vorpommern (`lod2_sources()`); for
  another state name its open portal instead of a number.
- `street=` keeps one street's buildings.""",
        template='''\
place = await geocode(query="Regensburg Altstadt")
assert place.get("ok"), place
lod2 = await fetch_lod2(bbox=place["bbox"], output_path="lod2.gpkg")
assert lod2.get("ok"), lod2''',
    ),
)

#: What every task needs to know whatever its sources — always in the coder's instructions.
ANALYSIS_NOTES = """\
## Facts that hold for every task

- A travel time ("10-minute walk", "catchment of a stop") is `service_area` on the street
  network, never a `vector_buffer` — a radius ignores rivers, railways and dead ends. No
  network, no answer: say so instead of substituting a radius.
- A selection ("the ten tallest", "the stops inside X") is written to its own layer and
  mapped as that layer, not shown by colouring the whole area.
- `render_map` with `ok: false` and `embedded: false` wrote no file: there is no map to link.
- `geocode` may return `ambiguous` with `candidates`: check `display_name` and `area_km2`
  before using its bbox.
- A result that carries a `licence` names its source: state source and licence in one
  sentence of the answer.
"""

BY_ID = {h.id: h for h in HANDBOOKS}


def source_index() -> str:
    """The short index the planner chooses from — one line per source."""
    return "\n".join(f"- `{h.id}`: {h.summary}" for h in HANDBOOKS)


def render(ids: list[str]) -> str:
    """The handbooks of the chosen sources, for the coder's instructions."""
    parts = []
    for source in dict.fromkeys(ids):
        h = BY_ID.get(source)
        if h is None:
            continue
        parts.append(f"### Source `{h.id}`\n{h.summary}\n\nFunctions: "
                     + ", ".join(f"`{f}`" for f in h.functions)
                     + f"\n\n{h.notes}\n\nTemplate (adapt it, do not copy blindly):\n"
                     + f"```python\n{h.template}\n```")
    return "\n\n".join(parts)


def allowed_functions(ids: list[str], all_names: set[str]) -> set[str]:
    """Every tool except the source functions of sources that were *not* chosen."""
    chosen = {f for i in ids if i in BY_ID for f in BY_ID[i].functions}
    sourced = {f for h in HANDBOOKS for f in h.functions}
    return {n for n in all_names if n not in sourced or n in chosen or n in ALWAYS}
