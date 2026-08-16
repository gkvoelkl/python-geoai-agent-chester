"""GeoConnectorsCapability — container connectors (GeoPackage / SpatiaLite / PostGIS).

The *query* connectors (geocode, OSM, STAC, DEM) produce one dataset per request
and live in ``discovery.py``. **Container** connectors are different: they hold an
*enumerable* set of existing datasets you **list, then pull** (doc/geodata-concept
§3.2). This capability gives the agent the uniform trio for them —

- `geoconnectors_list()`            — what sources are reachable (and their kind)
- `geodatasets_list(connector)`     — the layers/tables a container exposes
- `geodataset_describe(connector, dataset)` — one dataset's columns + extent
- `geodataset_fetch(connector, dataset, output, bbox?, where?)` — pull a subset
  into a cache GeoPackage (where it ages like any download, with a sidecar)

Two backends:
- **File containers** (`.gpkg`, `.sqlite`/SpatiaLite): zero-config, read via OGR
  (pyogrio/geopandas). No raw SQL — bbox is pushed to OGR as a numeric window and
  attribute `where` is applied in pandas, so a model-generated filter can't inject.
- **PostGIS**: configured by DSN + schema. Read-only, via `GeoDataFrame.from_postgis`
  with **bound parameters**, the table **whitelisted** to what `geodatasets_list`
  returns, and a parameterised `ST_MakeEnvelope(..., srid)` bbox (doc §4.3 safety).
  sqlalchemy/psycopg are imported lazily, so the file path works without them.

The capability is **inert when unconfigured** (no roots, no PostGIS DSN): it still
lets the user name a file container by path, but advertises nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import geofacts, provenance
from chester.capabilities.discovery import _apply_where
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

# Vector container file types we treat as OGR-readable containers.
_FILE_CONTAINER_EXTS = {".gpkg", ".sqlite", ".db"}
# The query connectors, for awareness in geoconnectors_list (they live elsewhere).
_QUERY_CONNECTORS = [
    {"name": "geocode", "kind": "query", "tool": "geocode"},
    {"name": "osm", "kind": "query", "tool": "osm_features"},
    {"name": "stac", "kind": "query", "tool": "stac_search/fetch_raster"},
    {"name": "dem", "kind": "query", "tool": "fetch_dem"},
]

_INSTRUCTIONS = """\
## Container connectors (the user's own data collections)

Some sources hold *many* datasets you list then pull — GeoPackages, SpatiaLite
files, PostGIS schemas. Use these when the user points Chester at their own data
(see the `connect-data` skill):
- `geoconnectors_list()` — what's reachable (query connectors + configured
  containers).
- `geodatasets_list(connector)` — the layers/tables in a container. `connector`
  is a file path (`.gpkg`/`.sqlite`) or `"postgis"` if configured.
- `geodataset_describe(connector, dataset)` — one dataset's columns and extent.
- `geodataset_fetch(connector, dataset, output, bbox?, where?)` — pull a subset
  into the cache (it ages like any download). Keep `bbox`/`where` tight — pull a
  window of interest, not a whole table.

The user's master data is precious: prefer **referencing it in place** (a
read-only data root, never pruned) over copying. Only fetched working copies age.\
"""


# ── file-container backend (OGR via pyogrio/geopandas) ───────────────────────


def is_file_container(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _FILE_CONTAINER_EXTS


def file_datasets(path: str) -> list[dict]:
    """Each layer of a file container with CRS, geometry type, count and extent."""
    out: list[dict[str, object]] = []
    for layer in geofacts.list_layers(path):
        try:
            f = geofacts.vector_facts(path, layer=layer)
        except Exception as exc:  # noqa: BLE001 - report the layer, not a crash
            out.append({"dataset": layer, "error": f"{type(exc).__name__}: {exc}"})
            continue
        entry: dict[str, object] = {
            "dataset": layer,
            "crs": f.get("crs"),
            "geometry_type": (f.get("geometry_types") or [None])[0],
            "features": f.get("feature_count"),
            "extent_wgs84": f.get("bounds_wgs84"),
        }
        out.append(entry)
    return out


def file_describe(path: str, dataset: str) -> dict:
    """Columns (name→dtype), feature count, CRS and extent of one layer."""
    from pyogrio import read_info

    info = read_info(path, layer=dataset)
    f = geofacts.vector_facts(path, layer=dataset)
    # pyogrio returns numpy arrays here, so coerce to plain lists (no truthiness).
    fields = [str(c) for c in info.get("fields", [])]
    dtypes = [str(d) for d in info.get("dtypes", [])]
    return {
        "dataset": dataset,
        "crs": f.get("crs"),
        "geometry_type": (f.get("geometry_types") or [None])[0],
        "features": f.get("feature_count"),
        "extent_wgs84": f.get("bounds_wgs84"),
        "columns": dict(zip(fields, dtypes)) if dtypes else {c: "?" for c in fields},
    }


def file_fetch(path: str, dataset: str, output: str,
               bbox: list[float] | None = None, where: dict | None = None) -> dict:
    """Subset one layer of a file container into ``output`` (a GeoPackage).

    ``bbox`` (WGS84 [w,s,e,n]) is pushed to OGR as a numeric read window;
    ``where`` is an exact-match attribute dict applied in pandas (no SQL).
    """
    import geopandas as gpd

    read_kwargs: dict[str, Any] = {"layer": dataset}
    if bbox:
        # pyogrio's bbox window is in the layer's own CRS; reproject the WGS84
        # bbox to it (a numeric window — never a SQL string).
        from pyogrio import read_info
        from pyproj import CRS, Transformer

        crs = read_info(path, layer=dataset).get("crs")
        if crs and not CRS.from_user_input(crs).is_geographic:
            tr = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_user_input(crs),
                                      always_xy=True)
            _w, _s, _e, _n = bbox
            read_kwargs["bbox"] = tuple(tr.transform_bounds(_w, _s, _e, _n))
        else:
            read_kwargs["bbox"] = tuple(bbox)

    gdf = gpd.read_file(path, **read_kwargs)
    if where:
        gdf, missing = _apply_where(gdf, where)
        if missing:
            return {"ok": False, "error": f"where references unknown column(s) {missing}"}
    if gdf.empty:
        return {"ok": False, "error": "selection matched 0 features"}
    gdf.to_file(output, driver="GPKG", layer=dataset)
    return {"ok": True, "output": output, "features": len(gdf),
            "crs": gdf.crs.to_string() if gdf.crs else None}


# ── PostGIS backend (lazy; bound params + whitelist + ST_MakeEnvelope) ───────


def _pg_engine(dsn: str):
    from sqlalchemy import create_engine

    return create_engine(dsn)


def pg_datasets(dsn: str, schema: str) -> list[dict]:
    """Spatial tables in ``schema`` from PostGIS ``geometry_columns`` (read-only)."""
    from sqlalchemy import text

    sql = text(
        "SELECT f_table_name, f_geometry_column, srid, type "
        "FROM geometry_columns WHERE f_table_schema = :schema "
        "ORDER BY f_table_name"
    )
    eng = _pg_engine(dsn)
    out = []
    with eng.connect() as con:
        con.exec_driver_sql("SET TRANSACTION READ ONLY")
        for row in con.execute(sql, {"schema": schema}).mappings():
            out.append({
                "dataset": row["f_table_name"],
                "geometry_column": row["f_geometry_column"],
                "srid": row["srid"],
                "geometry_type": row["type"],
            })
    return out


def _pg_whitelist(dsn: str, schema: str, dataset: str) -> dict | None:
    """Return the table's spatial metadata iff ``dataset`` is a real table here."""
    for d in pg_datasets(dsn, schema):
        if d["dataset"] == dataset:
            return d
    return None


def pg_fetch(dsn: str, schema: str, dataset: str, output: str,
             bbox: list[float] | None = None, where: dict | None = None) -> dict:
    """Subset a PostGIS table into ``output`` (GeoPackage), read-only and bound.

    The table is whitelisted to ``geodatasets_list`` names; the bbox uses a
    parameterised ``ST_MakeEnvelope``; ``where`` columns are whitelisted to the
    table's real columns and their values are bound parameters — no SQL is ever
    string-concatenated from model input.
    """
    import geopandas as gpd
    from sqlalchemy import text

    meta = _pg_whitelist(dsn, schema, dataset)
    if meta is None:
        return {"ok": False, "error": f"unknown table '{dataset}' in schema '{schema}'"}
    geom_col = meta["geometry_column"]
    eng = _pg_engine(dsn)

    # Identifiers can't be bound; they come only from the whitelist / real columns.
    from sqlalchemy import inspect

    real_cols = {c["name"] for c in inspect(eng).get_columns(dataset, schema=schema)}
    clauses: list[str] = []
    params: dict[str, object] = {}
    if bbox:
        clauses.append(
            f'ST_Intersects("{geom_col}", '
            "ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326))"
        )
        params.update(minx=bbox[0], miny=bbox[1], maxx=bbox[2], maxy=bbox[3])
    if where:
        bad = [c for c in where if c not in real_cols]
        if bad:
            return {"ok": False, "error": f"where references unknown column(s) {bad}"}
        for i, (col, val) in enumerate(where.items()):
            clauses.append(f'"{col}" = :w{i}')
            params[f"w{i}"] = val
    sql = f'SELECT * FROM "{schema}"."{dataset}"'
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    with eng.connect() as con:
        con.exec_driver_sql("SET TRANSACTION READ ONLY")
        gdf = gpd.GeoDataFrame.from_postgis(text(sql), con, geom_col=geom_col, params=params)
    if gdf.empty:
        return {"ok": False, "error": "selection matched 0 features"}
    gdf.to_file(output, driver="GPKG", layer=dataset)
    return {"ok": True, "output": output, "features": len(gdf),
            "crs": gdf.crs.to_string() if gdf.crs else None}


@dataclass
class GeoConnectorsCapability(AbstractCapability[Any]):
    """List and pull from container connectors (GeoPackage / SpatiaLite / PostGIS)."""

    workspace: str = DEFAULT_WORKSPACE
    roots: list[str] = field(default_factory=list)
    postgis: dict | None = None  # {"dsn": ..., "schema": ...}

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def _postgis_ready(self) -> bool:
        return bool(self.postgis and self.postgis.get("dsn"))

    def _discover_file_connectors(self) -> list[dict]:
        found = []
        for root in self.roots:
            base = os.path.abspath(os.path.expanduser(root))
            if not os.path.isdir(base):
                continue
            for dirpath, _dirs, files in os.walk(base):
                for name in files:
                    if is_file_container(name):
                        p = os.path.join(dirpath, name)
                        found.append({"name": p, "kind": "container/file"})
        return found

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace

        def _resolve_connector(connector: str):
            """(kind, handle) for a connector name: 'postgis' or a file path."""
            if connector == "postgis":
                if not self._postgis_ready():
                    return "error", "PostGIS is not configured (geodata.postgis.dsn)"
                return "postgis", self.postgis
            path = resolve_path(connector, ws)
            if not os.path.exists(path):
                return "error", f"no such container: {connector}"
            if not is_file_container(path):
                return "error", f"not a GeoPackage/SpatiaLite container: {connector}"
            return "file", path

        def geoconnectors_list() -> dict:
            """List reachable connectors: the query connectors plus any configured
            container connectors (file containers under data roots, and PostGIS)."""
            containers = self._discover_file_connectors()
            # Narrow locally instead of via `_postgis_ready()`: a helper's bool tells
            # a type checker nothing about the attribute it inspected.
            pg = self.postgis
            if pg and pg.get("dsn"):
                containers.append({
                    "name": "postgis", "kind": "container/postgis",
                    "schema": pg.get("schema", "public"),
                })
            return {"ok": True, "query_connectors": _QUERY_CONNECTORS,
                    "container_connectors": containers}

        def geodatasets_list(connector: str) -> dict:
            """List the datasets (layers/tables) a container connector exposes.

            ``connector`` is a file path (`.gpkg`/`.sqlite`) or `"postgis"`.
            """
            kind, handle = _resolve_connector(connector)
            if kind == "error":
                return {"ok": False, "error": handle}
            try:
                if kind == "file":
                    datasets = file_datasets(handle)
                else:
                    datasets = pg_datasets(handle["dsn"], handle.get("schema", "public"))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {"ok": True, "connector": connector, "kind": kind,
                    "count": len(datasets), "datasets": datasets}

        def geodataset_describe(connector: str, dataset: str) -> dict:
            """Describe one dataset of a container: columns, CRS, extent, count."""
            kind, handle = _resolve_connector(connector)
            if kind == "error":
                return {"ok": False, "error": handle}
            try:
                if kind == "file":
                    return {"ok": True, **file_describe(handle, dataset)}
                meta = _pg_whitelist(handle["dsn"], handle.get("schema", "public"), dataset)
                if meta is None:
                    return {"ok": False, "error": f"unknown table '{dataset}'"}
                return {"ok": True, **meta}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        def geodataset_fetch(
            connector: str,
            dataset: str,
            output: str,
            bbox: list[float] | None = None,
            where: dict | None = None,
        ) -> dict:
            """Pull a subset of a container dataset into a cache GeoPackage.

            ``bbox`` is [west, south, east, north] in WGS84; ``where`` is an
            exact-match attribute dict. The output ages like any download and gets
            a provenance sidecar. Keep the selection tight (a window of interest).
            """
            kind, handle = _resolve_connector(connector)
            if kind == "error":
                return {"ok": False, "error": handle}
            output = resolve_path(output, ws)
            try:
                if kind == "file":
                    r = file_fetch(handle, dataset, output, bbox=bbox, where=where)
                    src_name = os.path.basename(handle)
                else:
                    r = pg_fetch(handle["dsn"], handle.get("schema", "public"),
                                 dataset, output, bbox=bbox, where=where)
                    src_name = "postgis"
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output, source=f"connector/{kind}", tool="geodataset_fetch",
                    query={"connector": src_name, "dataset": dataset,
                           "bbox": bbox, "where": where},
                    crs=r.get("crs"),
                )
            return r

        return FunctionToolset(tools=[
            geoconnectors_list, geodatasets_list, geodataset_describe, geodataset_fetch,
        ])
