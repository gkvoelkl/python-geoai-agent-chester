"""The ressort cut — which tool of the wrapper layer belongs to which ressort.

chester-team is a multi-agent: an orchestrator calls ressort agents as tools, and
each ressort sees only its slice of the tools (`internal/TODO.md`, KP.5 T1). This
module is that slice, as data: names only, no imports of the tools, no framework. It
sits in chester-geo-tools so every adapter can use the same cut — the ressort agents
in chester-team, and Chester-MCP if it ever groups its catalogue.

**Cut along the phases of the chain, not along domains** (the concept's rule): find →
acquire → compute (vector | raster) → present. Geo work is a chain, and a ressort
must be able to run a whole piece of it — clip → buffer → dissolve is one thought;
handing over in the middle of it pays a handoff for nothing.

**Checking is no ressort** (the concept's rule, and why it holds). The check tools are
in :data:`CHECKS` and every ressort gets them (:func:`tools_for`):

1. *A result is judged where it was made.* A check needs the step's intent — which
   CRS was meant, which extent, which column. The ressort that did the work has it; a
   separate checker would get paths and have to guess, or be told, which is the
   handoff the phase cut exists to avoid.
2. *Ressorts are phases of the chain*; checking is a duty that runs through all of
   them, not a phase of its own.
3. *The independent judgement already exists, twice, and both are more independent
   than a checker ressort would be* — it would run on the same model under the same
   orchestrator. The **gate** is mechanical and can force a retry; the **judge** of
   Test-Level 3 is a different model of a different lineage.
4. *Most checks are arithmetic, not thought* (`check_crs`, `sanity_check_result`). An
   agent run with its own prefill for a function call would be expensive for nothing.

**The honest counterpoint:** one check does need a model — the visual one
(`inspect_map`, which renders and asks a vision model). Today every ressort has it;
it is the plausible candidate for a checker ressort if we ever want one.

**What would overturn this:** the runs say so. `tools_called` per ressort carries the
check tools (`team-runs/ressort-calls.jsonl`), so "everyone checks their own work" is
measurable. If the ressorts barely call them, the alternatives are: the orchestrator
checks after each ressort — it has the tools — or a checker ressort after all.

Borderline calls, made 2026-09-19 and open to revision on measurement:

* Listing and searching is *scout* (``*_sources``, ``*_search``, ``gtfs_feeds``,
  ``*_levels``, ``geocache_*``) — it answers "what is there".
* Everything that brings data into the cache is *acquisition*, including
  ``stats_table``, ``geodataset_fetch`` and the two converters (``cityjson_to_geopackage``,
  ``pointcloud_to_copc``), which produce the layer the next ressort works on.
* ``osm_features`` and ``wfs_features`` stay with *scout*, as in the concept: both are
  query-shaped, and the scout needs a first look at the data to judge a source.

Scope is the **wrapper layer** only. The agent-level tools outside it —
``geo_python_run``, ``inspect_map``, the ``qgis_*`` family, SelmaKit's ``write_plan``
and ``read_tool_result`` — are for the ressort agents' wrapper to place (KP.5 T2).
``tests/test_ressortcut.py`` holds the cut to the real tool surface: every tool in
exactly one place, no name that does not exist.
"""

from __future__ import annotations

#: Ressort → its tools, in the order of the chain.
RESSORTS: dict[str, tuple[str, ...]] = {
    "scout": (
        "geodata_search", "geoconnectors_list", "geodatasets_list", "geodataset_describe",
        "stac_catalogs", "stac_search", "wfs_capabilities", "wfs_features",
        "wms_capabilities", "osm_features", "geocode", "region_profile",
        "region_hierarchy", "boundaries_levels", "swiss_boundaries_levels",
        "austria_boundaries_levels", "lod2_sources", "pointcloud_search", "gtfs_feeds",
        "stats_sources", "stats_search", "geocache_list", "geocache_sync", "geocache_note",
    ),
    "acquisition": (
        "fetch_vector", "fetch_raster", "fetch_dem", "fetch_dgm1", "fetch_dop",
        "fetch_swissalti3d", "fetch_austria_dem", "fetch_swisstlmregio",
        "fetch_boundaries", "fetch_swiss_boundaries", "fetch_austria_boundaries",
        "fetch_lod2", "fetch_cityjson", "fetch_swissbuildings3d", "fetch_vienna_buildings",
        "fetch_pointcloud", "fetch_gtfs_stops", "fetch_gtfs_routes", "fetch_wms_map",
        "geodataset_fetch", "stats_table", "cityjson_to_geopackage", "pointcloud_to_copc",
    ),
    "vector": (
        "vector_info", "vector_filter", "vector_overlay", "vector_split_by_geometry",
        "vector_reproject", "vector_buffer", "vector_clip", "vector_intersection",
        "vector_extract_by_location", "vector_extract_by_attribute", "vector_dissolve",
        "vector_merge", "vector_join", "vector_add_field", "vector_field_sum",
        "service_area",
    ),
    "raster": (
        "rasterize", "sample_raster", "zonal_stats", "raster_calc", "slope", "aspect",
        "hillshade", "ruggedness", "fill_sinks", "flow_accumulation", "spectral_index",
        "detect_water",
    ),
    "output": ("render_map", "render_buildings_3d"),
}

#: The check tools — visible to every ressort, owned by none. The same four the single
#: agent has (`GeoValidationCapability`).
CHECKS: tuple[str, ...] = (
    "check_crs", "sanity_check_result", "check_topology", "cross_check",
)

#: Wrapper tools that only Chester-MCP serves, and that no agent inside this process
#: has any use for (decided 2026-09-20). They are named here so the cut stays complete
#: — every wrapper tool has exactly one place — without handing them to a ressort:
#:
#: * ``validate_result`` is the gate **without enforcement**, built for a foreign
#:   client that Chester cannot make retry. The orchestrator has the real, enforcing
#:   gate, exactly as chester-agent does; a second route to the same checks would only
#:   invite the model to take the one that costs nothing.
#: * ``read_artifact`` hands back file *contents* because an MCP client cannot read
#:   Chester's cache. Team and ressorts run in this process and read the files
#:   directly; looking at a map is `inspect_map` (chester-runtime).
MCP_ONLY: tuple[str, ...] = ("validate_result", "read_artifact")


def ressort_of(tool: str) -> str | None:
    """The ressort a tool belongs to, ``"checks"``/``"mcp-only"`` for those, else ``None``."""
    if tool in CHECKS:
        return "checks"
    if tool in MCP_ONLY:
        return "mcp-only"
    return next((name for name, tools in RESSORTS.items() if tool in tools), None)


def tools_for(ressort: str) -> tuple[str, ...]:
    """Everything a ressort agent gets: its own slice plus the check tools."""
    return RESSORTS[ressort] + CHECKS
