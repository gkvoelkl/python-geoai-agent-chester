"""What the wrapper layer's tools return — typed, so a model sees the shape (Phase WB).

Every wrapper tool is annotated ``-> dict`` and describes its result in uneven prose:
`osm_features` never said that `features` is a *count* and the output EPSG:4326 — gemma4
wrote ``len(r["features"])`` (2026-10-24), Nemotron sent ``"None"`` for an omitted
parameter (2026-10-25). Each class is one tool's result **on success**; on failure every
tool returns ``{"ok": False, "error": ...}``. Key sets read off 1 000+ logged results and
the code, held against real returns by `tests/test_toolreturns.py`.

Pure typing, no pydantic import: ``__pydantic_config__`` is a plain dict pydantic reads,
so attribute docstrings become field descriptions — CodeMode prints them in the stub
(undeclared keys still reach the program, nothing is dropped). No ``from __future__ import
annotations``: with string annotations a ``TypedDict`` cannot see ``NotRequired`` and
marks every key required.

**No ``| None`` and no ``NotRequired`` on a field a program passes on.** CodeMode
type-checks every program against the stubs: ``bbox: list[float] | None`` made it reject
``fetch_dem(bbox=place["bbox"])`` twice (2026-11-05), ``wfs_url: NotRequired[str]`` made
it reject ``r.get("wfs_url")`` (2026-11-07). Such fields are required and declared
without ``None``; the tool sends them as null where they do not apply, the field
description says so, and :data:`NULLABLE` lists them for the test.
"""

from collections.abc import Callable
from typing import Any, NotRequired, TypedDict

_CONFIG = {"use_attribute_docstrings": True}


class ToolResult(TypedDict):
    """Keys any tool may carry."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    ok: bool
    """True on success. On failure only ``ok: False`` and ``error`` are set."""
    warning: NotRequired[str]
    """A caveat to repeat in the answer — the result is usable but limited."""
    note: NotRequired[str]
    """Background on how the result was made."""


class GeocodeResult(ToolResult):
    """Result of ``geocode``: where a place is."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    query: str
    display_name: str
    """The matched place, e.g. "Bodenmais, Landkreis Regen, Bavaria, Germany"."""
    bbox: list[float]
    """[west, south, east, north] in WGS84 degrees — pass it to bbox-taking tools.
    Null only for a bare Photon point match (then ``note`` says so): use ``centroid``."""
    centroid: list[float]
    """[lon, lat] in WGS84 degrees."""
    crs: str
    match_class: str
    match_type: str
    boundary: str
    """Path of the boundary polygon file — null unless ``output_path`` was given."""
    area_km2: NotRequired[float]
    area_basis: NotRequired[str]
    bbox_area_km2: NotRequired[float]
    ambiguous: NotRequired[bool]
    """True when several places match: check ``candidates`` before using the bbox."""
    candidates: NotRequired[list[dict[str, Any]]]
    source: NotRequired[str]


class OsmFeaturesResult(ToolResult):
    """Result of ``osm_features``: OpenStreetMap features written to a file."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    output: str
    """Path of the written layer, EPSG:4326 — reproject before any distance or area."""
    features: int
    """Number of features written — a count, not a list."""
    geometry_types: list[str]
    crs: str
    mixed_geometry: NotRequired[bool]
    """True when points and polygons are mixed: split with ``vector_split_by_geometry``."""
    clipped_to_place: NotRequired[bool]
    features_trimmed: NotRequired[int]
    features_dropped: NotRequired[int]
    features_split: NotRequired[int]
    area_outside_km2: NotRequired[float]


class BoundariesResult(ToolResult):
    """Result of ``fetch_boundaries``: official administrative polygons."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    output: str
    """Path of the boundary layer, EPSG:25832 (metric)."""
    dataset: str
    level: str
    """The administrative level delivered, e.g. GEM (municipality) or KRS (district)."""
    key_column: str
    """The join key column, e.g. AGS — the key for joining statistics."""
    units: int
    """Number of polygons — a count."""
    crs: str
    licence: str
    """Name the source and this licence in the answer."""
    level_inferred: NotRequired[bool]


class _RasterFetch(ToolResult):
    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    output: str
    """Path of the written GeoTIFF."""
    size: list[int]
    """[width, height] in pixels."""
    resolution_m: float
    crs: str
    tiles_used: int
    coverage: dict[str, Any]
    """How much of the requested bbox holds data (``covers_request``, ``data_share``)."""


class DemResult(_RasterFetch):
    """Result of ``fetch_dem``: Copernicus GLO-30, ~30 m, EPSG:4326 (degrees)."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    tiles_missing: NotRequired[list[str]]


class Dgm1Result(_RasterFetch):
    """Result of ``fetch_dgm1``: official 1 m terrain model, metric CRS (EPSG:25832/25833)."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    state: str
    """The federal state served, e.g. "BY"."""
    state_name: str
    tiles_missing: list[str]
    licence: str
    """Name the source and this licence in the answer."""


class SplitPart(TypedDict):
    """One geometry family of a split layer."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    geometry_type: str
    """E.g. "Point", "Polygon", "MultiPolygon"."""
    features: int
    output: str
    """Path of this part's file."""


class SplitResult(ToolResult):
    """Result of ``vector_split_by_geometry``: one file per geometry type."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    features: int
    """Features in the input — a count."""
    parts: list[SplitPart]
    """One entry per geometry type. Keep the polygon parts too — schools are often buildings."""


class VectorOpResult(ToolResult):
    """Result of ``vector_reproject``, ``vector_buffer``, ``vector_clip``."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    output: str
    """Path of the written layer — pass it to the next step."""
    features_in: int
    features_out: int
    crs: str


class MergeResult(VectorOpResult):
    """Result of ``vector_merge``: several layers stacked on a common CRS."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    layers: list[Any]
    mixed_geometry: NotRequired[bool]
    """True when the merged layer mixes points and polygons (then a ``warning`` explains)."""
    columns_only_in_some_layers: NotRequired[list[str]]
    """Columns not every input had — empty in the features of the other layers."""


class FilterResult(ToolResult):
    """Result of ``vector_filter``: the features matching an attribute expression."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    output: str
    before: int
    """Features before filtering — a count."""
    after: int
    """Features kept — a count."""


class ColumnValues(TypedDict):
    """The distinct values of one column — ``vector_info(values_of=...)``."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    column: str
    distinct: int
    values: list[str]
    """The values themselves: ``info["values"]["values"]``, not ``info["values"][column]``."""
    truncated: bool


class VectorInfoResult(ToolResult):
    """Result of ``vector_info``: a layer's schema, without changing it."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    features: int
    """Number of features — a count."""
    geometry_types: list[str]
    crs: str
    """E.g. "EPSG:4326" — null for a layer that declares no CRS."""
    columns: dict[str, Any]
    """The populated columns only."""
    columns_total: int
    columns_empty: int
    bounds: list[float]
    """[minx, miny, maxx, maxy] in the layer's CRS — null for an empty layer."""
    kind: NotRequired[str]
    values: NotRequired[ColumnValues]
    mixed_geometry: NotRequired[bool]


class SampleResult(ToolResult):
    """Result of ``sample_raster``: the raster value at each point."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    output: str
    """Path of the point layer with the sampled values in ``column``."""
    features: int
    column: str
    without_value: int
    """Points the raster does not cover — they carry null, never 0."""


class ZoneValue(TypedDict):
    """One zone: ``zone`` is its name (first text column) or its index."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    zone: str | int
    value: float
    coverage: float


class ZonalResult(ToolResult):
    """Result of ``zonal_stats``: one statistic per polygon."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    output: str
    """Path of the zone layer with the statistic in ``column`` and a ``coverage`` column."""
    zones: int
    column: str
    stat: str
    zones_without_data: int
    """Zones the raster does not reach — they carry null, never 0."""
    values: list[ZoneValue]
    """Each zone's statistic, at most 50 — read the numbers here, not from the file."""


class RenderMapResult(ToolResult):
    """Result of ``render_map``: an interactive HTML map."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    output: str
    """Path of the HTML map — copy it verbatim into the answer."""
    layers: list[Any]
    features: NotRequired[dict[str, Any]]
    styling: NotRequired[dict[str, Any]]
    picture: NotRequired[str]
    attribution: NotRequired[list[str]]
    choropleth: NotRequired[dict[str, Any]]
    wms: NotRequired[dict[str, Any]]


class CatalogResource(TypedDict):
    """One resource of a catalog dataset, classified by its real service type."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    service: str
    """"WFS", "WMS", "GeoJSON", "Shapefile", "GeoPackage", "CSV", … — there is no ``name``
    or ``format`` key: the type is ``service``, read off the URL."""
    url: str
    wfs_url: str
    """The WFS base URL — pass it as ``url`` to ``wfs_features``. Null unless ``service`` is
    "WFS"."""
    typename: str
    """The WFS layer — pass it as ``typename``. Null unless ``service`` is "WFS", and null
    when the URL names no layer: then list them with ``wfs_capabilities(url=wfs_url)``."""


class CatalogCandidate(TypedDict):
    """One dataset found in the catalog."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    title: str
    publisher: str
    license: str
    """Null when the terms are unverified — check before reuse."""
    resources: list[CatalogResource]


class CatalogSearchResult(ToolResult):
    """Result of ``geodata_search``: datasets in an open-data catalog, geospatial first."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    query: str
    catalog: str
    total: int
    """Matches in the catalog; ``count`` of them are listed."""
    count: int
    candidates: list[CatalogCandidate]


class WfsTypename(TypedDict):
    """One layer a WFS offers."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    name: str
    """The layer name to pass as ``typename`` to ``wfs_features``, e.g. "ms:kleingliederung"."""
    title: str
    bbox: list[float]
    """[west, south, east, north] in WGS84 — null when the service declares none."""
    crs: list[str]


class WfsCapabilitiesResult(ToolResult):
    """Result of ``wfs_capabilities``: the layers of a WFS service."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    url: str
    version: str
    title: str
    count: int
    typenames: list[WfsTypename]
    """One entry per layer — the name to fetch is ``typenames[i]["name"]``."""


class WfsFeaturesResult(ToolResult):
    """Result of ``wfs_features``: one WFS layer written to a file."""

    __pydantic_config__ = _CONFIG  # type: ignore[misc]
    output: str
    """Path of the written layer, in the service's CRS (see ``crs``)."""
    features: int
    """Number of features written — a count, not a list."""
    geometry_types: list[str]
    crs: str
    """E.g. "EPSG:25832" — null when the service declares none."""
    mixed_geometry: NotRequired[bool]


#: Fields declared without ``None`` for the type checker that can still be null at runtime.
NULLABLE: dict[str, frozenset[str]] = {
    "GeocodeResult": frozenset({"bbox", "boundary"}),
    "VectorInfoResult": frozenset({"crs", "bounds"}),
    "ZoneValue": frozenset({"value"}),
    "CatalogResource": frozenset({"wfs_url", "typename"}),
    "CatalogCandidate": frozenset({"title", "publisher", "license"}),
    "WfsCapabilitiesResult": frozenset({"title"}),
    "WfsTypename": frozenset({"title", "bbox"}),
    "WfsFeaturesResult": frozenset({"crs"}),
}

#: Tool name → its result type. Tools not named here keep ``-> dict``.
RETURNS: dict[str, type] = {
    "geocode": GeocodeResult,
    "osm_features": OsmFeaturesResult,
    "fetch_boundaries": BoundariesResult,
    "fetch_dem": DemResult,
    "fetch_dgm1": Dgm1Result,
    "vector_split_by_geometry": SplitResult,
    "vector_merge": MergeResult,
    "vector_reproject": VectorOpResult,
    "vector_buffer": VectorOpResult,
    "vector_clip": VectorOpResult,
    "vector_filter": FilterResult,
    "vector_info": VectorInfoResult,
    "sample_raster": SampleResult,
    "zonal_stats": ZonalResult,
    "render_map": RenderMapResult,
    "geodata_search": CatalogSearchResult,
    "wfs_capabilities": WfsCapabilitiesResult,
    "wfs_features": WfsFeaturesResult,
}


def typed(tool: Callable[..., Any]) -> Callable[..., Any]:
    """Give ``tool`` its result type from :data:`RETURNS`, if it has one; return it.

    Sets the return annotation in place — the tool modules stay as they are, and a tool
    without an entry is left untouched.
    """
    result = RETURNS.get(getattr(tool, "__name__", ""))
    if result is not None:
        tool.__annotations__["return"] = result
    return tool
