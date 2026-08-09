"""GeoBoundariesCapability — official German/EU administrative boundaries (BKG).

The geometry half of the **official-statistics → choropleth** workflow. Chester's
statistics connectors deliver tables keyed by AGS / NUTS code but no geometry;
this capability fetches the authoritative boundary polygons (BKG open
Verwaltungsgebiete, DL-DE→BY 2.0) carrying exactly those keys, so a stats table
joins straight onto the polygons. Thin agent layer over ``chester/boundaries.py``:

- ``boundaries_levels()`` — the fetchable admin levels and their join keys.
- ``fetch_boundaries(level, output_path, match?, bbox?)`` — a boundary subset as a
  GeoPackage (with a provenance sidecar).

Joining a stats table onto the result is a normal QGIS step
(``native:joinattributestable`` on AGS / NUTS_CODE) — no bespoke join tool, in
keeping with the statistics-connector design.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import austria, boundaries, provenance, swisstopo
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

_INSTRUCTIONS = """\
## Official boundaries (Verwaltungsgebiete) — geometry for choropleths & clipping

When you need **official administrative polygons** — for a choropleth of an
official-statistics table, or a clean clip mask — fetch them from the BKG
(authoritative, open):

- `boundaries_levels()` — the levels: German `STA`/`LAN`/`RBZ`/`KRS`/`VWG`/`GEM`
  (keyed by **AGS**) and EU `NUTS1`/`NUTS2`/`NUTS3` (keyed by **NUTS_CODE**).
- `fetch_boundaries(level, output_path, match?, bbox?)` — `match` filters by key
  prefix ("09" = Bayern, "09162" = München) **or** name ("München"); `bbox`
  [w,s,e,n] WGS84 windows it. Output is a GeoPackage in EPSG:25832 with the join
  key column.

**Choropleth from statistics:** `stats_table(...)` → `fetch_boundaries(level=…)`
matching the table's granularity → join the table onto the polygons with
`qgis_run("native:joinattributestable")` on the shared key (AGS ↔ the stats key,
or NUTS_CODE) → `render_map(column=<value>)`. Match the level to the stats key:
Gemeinde figures → `GEM` (AGS), Kreis → `KRS`, Eurostat NUTS-3 → `NUTS3`. Prefer
these official polygons over an OSM boundary for administrative areas.

**Switzerland (swissBOUNDARIES3D, EPSG:2056):** for Swiss administrative areas use
`fetch_swiss_boundaries(level, output_path, match?, bbox?, canton?)` with `level` one of
`LAND`/`KANTON`/`BEZIRK`/`GEMEINDE` (`swiss_boundaries_levels()` lists them). For **all
units of a canton** (e.g. "die Gemeinden im Kanton Bern") pass `canton="Bern"` (or its
number) — do **NOT** use `match` for this: the Swiss `bfs_nummer` is not hierarchical, so
`match="Bern"` would only return units *named* "Bern" (across any canton), not the
canton's members. `match` is for finding a *named* unit; `bbox` [w,s,e,n] WGS84 windows
it. The GEMEINDE layer already carries `einwohnerzahl` (population) and `bfs_nummer` (the
Swiss statistics key) — a canton population choropleth is just
`fetch_swiss_boundaries("GEMEINDE", …, canton="Bern")` → `render_map(column="einwohnerzahl")`,
no separate stats table needed.

**Austria (STATISTIK AUSTRIA, EPSG:31287):** for Austrian administrative areas use
`fetch_austria_boundaries(level, output_path, match?, bbox?)` with `level` one of
`GEM`/`BEZIRK`/`NUTS1`/`NUTS2`/`NUTS3` (`austria_boundaries_levels()` lists them). The
join key is `g_id` (GKZ for GEM) and it is **hierarchical** like the German AGS, so
`match` by key prefix works — "7" = Tirol, "701" = Bezirk Innsbruck — or by name
("Innsbruck"); `bbox` [w,s,e,n] WGS84 windows it. Output is EPSG:31287 (MGI/Austria
Lambert), metric.\
"""


@dataclass
class GeoBoundariesCapability(AbstractCapability[Any]):
    """Fetch official German/EU administrative boundaries from the BKG."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace

        def boundaries_levels() -> dict:
            """List the fetchable administrative levels and their join keys
            (German AGS levels + EU NUTS levels), from the BKG Verwaltungsgebiete."""
            return {"ok": True, "levels": boundaries.levels_catalog(),
                    "licence": boundaries._BKG_LICENCE,
                    "note": "German levels keyed by AGS; NUTS levels by NUTS_CODE. "
                    "Join a stats_table onto these with native:joinattributestable."}

        def fetch_boundaries(
            level: str,
            output_path: str,
            match: str | None = None,
            bbox: list[float] | None = None,
            land_only: bool = True,
        ) -> dict:
            """Fetch official administrative boundary polygons into a GeoPackage.

            ``level`` = STA/LAN/RBZ/KRS/VWG/GEM (German, AGS) or NUTS1/NUTS2/NUTS3
            (EU, NUTS_CODE). ``match`` filters by key prefix ("09" = Bayern) or
            name substring ("München"); ``bbox`` = [west, south, east, north] in
            WGS84 windows the result. ``land_only`` (default) keeps the GF=4 land
            polygons, dropping water-body variants. The output (EPSG:25832) carries
            the join key so a statistics table joins straight onto it. Use for
            statistics choropleths and for clipping to an administrative area.
            """
            output_path = resolve_path(output_path, ws)
            cache_dir = str(resolve_path("_boundaries", ws))
            try:
                r = boundaries.fetch_boundaries(
                    level, output_path, cache_dir,
                    match=match, bbox=bbox, land_only=land_only,
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path, source="connector/bkg-vg", tool="fetch_boundaries",
                    query={"level": r["level"], "match": match, "bbox": bbox},
                    crs=r.get("crs"), licence=r.get("licence"),
                )
            return r

        def swiss_boundaries_levels() -> dict:
            """List the fetchable Swiss administrative levels and their join keys
            (LAND/KANTON/BEZIRK/GEMEINDE), from swissBOUNDARIES3D (swisstopo)."""
            return {"ok": True, "levels": swisstopo.swiss_boundary_levels(),
                    "licence": swisstopo._SWISSTOPO_LICENCE,
                    "note": "GEMEINDE keyed by bfs_nummer (the Swiss statistics key) "
                    "and carries einwohnerzahl (population). CRS EPSG:2056 (LV95)."}

        def fetch_swiss_boundaries(
            level: str,
            output_path: str,
            match: str | None = None,
            bbox: list[float] | None = None,
            canton: str | int | None = None,
            ch_only: bool = True,
        ) -> dict:
            """Fetch Swiss administrative boundary polygons into a GeoPackage.

            ``level`` = LAND / KANTON / BEZIRK / GEMEINDE (swissBOUNDARIES3D). To get
            **all units of a canton** (e.g. all Gemeinden of Kanton Bern) pass
            ``canton`` (name "Bern" or number 2) — the Swiss ``bfs_nummer`` is **not**
            hierarchical, so ``match`` can NOT select a canton's members. ``match``
            filters by name substring ("Bern") or key prefix — use it to find a *named*
            unit only. ``bbox`` = [west, south, east, north] in WGS84 windows the result.
            ``ch_only`` (default) keeps Swiss units, dropping Liechtenstein / foreign
            enclaves. The output (EPSG:2056, LV95) carries the join key; GEMEINDE also
            carries ``bfs_nummer`` and ``einwohnerzahl`` (population), so a Swiss
            population choropleth needs no separate stats table. The Swiss counterpart
            of ``fetch_boundaries``.
            """
            output_path = resolve_path(output_path, ws)
            cache_dir = str(resolve_path("_boundaries", ws))
            try:
                r = swisstopo.fetch_swissboundaries3d(
                    level, output_path, cache_dir,
                    match=match, bbox_wgs84=bbox, canton=canton, ch_only=ch_only,
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path, source="connector/swisstopo",
                    tool="fetch_swiss_boundaries",
                    query={"level": r["level"], "match": match, "canton": canton,
                           "bbox": bbox},
                    crs=r.get("crs"), licence=r.get("licence"),
                )
            return r

        def austria_boundaries_levels() -> dict:
            """List the fetchable Austrian administrative levels and their join key
            (GEM/BEZIRK/NUTS1/NUTS2/NUTS3), from STATISTIK AUSTRIA."""
            return {"ok": True, "levels": austria.austria_boundary_levels(),
                    "licence": austria._LICENCE,
                    "note": "Key column g_id (GKZ for GEM, hierarchical → prefix match "
                    "selects a Bundesland/Bezirk). CRS EPSG:31287 (MGI/Austria Lambert)."}

        def fetch_austria_boundaries(
            level: str,
            output_path: str,
            match: str | None = None,
            bbox: list[float] | None = None,
        ) -> dict:
            """Fetch Austrian administrative boundary polygons into a GeoPackage.

            ``level`` = GEM / BEZIRK / NUTS1 / NUTS2 / NUTS3 (STATISTIK AUSTRIA).
            ``match`` filters by join-key (``g_id``) prefix — hierarchical for GEM/BEZIRK,
            so "7" = Tirol, "701" = Bezirk Innsbruck — or by name ("Innsbruck"). ``bbox``
            = [west, south, east, north] in WGS84 windows the result. The output
            (EPSG:31287, MGI/Austria Lambert) carries ``g_id`` (join key, GKZ for GEM) and
            ``g_name``. The Austrian counterpart of ``fetch_boundaries`` /
            ``fetch_swiss_boundaries``.
            """
            output_path = resolve_path(output_path, ws)
            cache_dir = str(resolve_path("_at_boundaries", ws))
            try:
                r = austria.fetch_austria_boundaries(
                    level, output_path, cache_dir, match=match, bbox_wgs84=bbox)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path, source="connector/statistik-austria",
                    tool="fetch_austria_boundaries",
                    query={"level": r["level"], "match": match, "bbox": bbox},
                    crs=r.get("crs"), licence=r.get("licence"),
                )
            return r

        return FunctionToolset(tools=[boundaries_levels, fetch_boundaries,
                                      swiss_boundaries_levels, fetch_swiss_boundaries,
                                      austria_boundaries_levels, fetch_austria_boundaries])
