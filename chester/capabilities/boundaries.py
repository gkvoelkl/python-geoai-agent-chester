"""GeoBoundariesCapability — official German/EU administrative boundaries (BKG).

The geometry half of the **official-statistics → choropleth** workflow. Chester's
statistics connectors deliver tables keyed by AGS / NUTS code but no geometry;
this capability fetches the authoritative boundary polygons (BKG open
Verwaltungsgebiete, DL-DE→BY 2.0) carrying exactly those keys, so a stats table
joins straight onto the polygons. Thin agent layer over ``chester/boundaries.py``:

- ``boundaries_levels()`` — the fetchable admin levels and their join keys.
- ``fetch_boundaries(level, output_path, match?, bbox?)`` — a boundary subset as a
  GeoPackage (with a provenance sidecar).

Joining a stats table onto the result is one call to ``vector_join`` (on AGS or
NUTS_CODE). That join is where these polygons are usually lost: a key read as a
number drops the leading zero of every Bavarian AGS, and the result looks complete
while being empty — which is why ``vector_join`` reports what it matched.
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

- `fetch_boundaries(output_path, match=…)` — **one call, no level needed.** The
  level is inferred from the match, smallest unit first, and reported back:
  `fetch_boundaries("tegernheim.gpkg", match="Tegernheim")` returns the Gemeinde.
  `match` filters by key prefix ("09" = Bayern, "09162" = München) **or** name.
  Output is a GeoPackage in EPSG:25832 with the join key column.
- Pass `level=` only when you need a *particular* one — the Landkreis Regensburg
  rather than the city of the same name, or a level matching a statistics table's
  granularity. `boundaries_levels()` lists them: German `STA`/`LAN`/`RBZ`/`KRS`/
  `VWG`/`GEM` (keyed by **AGS**), EU `NUTS1`/`NUTS2`/`NUTS3` (by **NUTS_CODE**).

**Choropleth from statistics:** `stats_table(...)` → `fetch_boundaries(level=…)`
matching the table's granularity → join the table onto the polygons with
`vector_join(field=…)` on the shared key (AGS ↔ the stats key, or NUTS_CODE) →
`render_map(column=<value>)`. Read `vector_join`'s `joined`/`unjoined` before you
map: a choropleth over an unmatched join is a picture of nothing. Match the level to the stats key:
Gemeinde figures → `GEM` (AGS), Kreis → `KRS`, Eurostat NUTS-3 → `NUTS3`. Prefer
these official polygons over an OSM boundary for administrative areas.

**BKG stops at the Gemeinde (`GEM`).** A *Stadtbezirk*, *Stadtteil*, *Ortsteil*,
*Quartier* or *statistischer Bezirk* is **below** that level and simply is not in
this dataset — `fetch_boundaries` cannot deliver it and neither can a geocoder
(Nominatim answers a district name with some address inside it). For those areas
the route is `geodata_search("<Stadtbezirke Stadtname>")` → `wfs_features` /
`fetch_vector` from the city's own portal → pick the feature by name
(`vector_info(path, values_of="name")` → `vector_filter`). That is not a fallback,
it is the only authoritative source; do it *before* reaching for OSM.

**Never substitute a similar-sounding polygon.** OSM answers "Innenstadt" with
`Altstadt von Regensburg mit Stadtamhof` (a UNESCO world-heritage outline,
`heritage=1`), "Zentrum" with a neighbourhood point, "Bezirk" with a postal area.
A polygon whose `name` is not the area that was asked for is a different area —
using it silently makes every count and every share wrong. If nothing
authoritative can be found, say so and report what you did use.

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


#: Swiss levels whose members belong to a canton. Asking for these *by name* is the
#: documented trap: `bfs_nummer` is not hierarchical, so `match` cannot select a
#: canton's members the way a German AGS prefix can.
_CANTON_SCOPED_LEVELS = frozenset({"GEMEINDE", "BEZIRK"})


def _canton_confusion_warning(level, match, canton, units) -> str:
    """Warn when `match` was used where `canton` was meant.

    Measured 2026-09-05, `swiss-population-choropleth-bern`: the agent called
    ``fetch_swiss_boundaries(level="GEMEINDE", match="Bern", canton=None)``, got
    **4 units** — the municipalities *named* Bern, scattered across cantons — and
    rendered them as "Einwohnerzahl je Gemeinde im Kanton Bern". The count was in
    the result and nothing looked at it.

    The rule is already in the instructions, in capitals ("do **NOT** use `match`
    for this"), and the run before this one got it right. So this is not about
    teaching it: it is about catching the coin-flip at the moment it lands wrong,
    where a prompt rule cannot. Same device as the bbox warning.
    """
    if str(level or "").upper() not in _CANTON_SCOPED_LEVELS:
        return ""
    if not match or canton:
        return ""
    count = f"{units} unit(s)" if units is not None else "these units"
    return (
        f"`match` selects units whose *name* matches — it returned {count}. The Swiss "
        "bfs_nummer is NOT hierarchical, so `match` cannot select a canton's members "
        "the way a German AGS prefix can. If you wanted every unit of a canton, call "
        "again with `canton=` (name or number) and no `match`. If you did want the "
        "named unit, ignore this."
    )


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
            return {
                "ok": True,
                "levels": boundaries.levels_catalog(),
                "licence": boundaries._BKG_LICENCE,
                "note": "German levels keyed by AGS; NUTS levels by NUTS_CODE. "
                "Join a stats_table onto these with `vector_join`; it says how many "
                "keys matched.",
            }

        #: Smallest unit first. A name given without a level almost always means the
        #: smallest thing that carries it — "Tegernheim" is a Gemeinde, not a state —
        #: and this is also the order the prompt teaches for scope escalation.
        #: NUTS levels are deliberately absent: they are a *parallel* taxonomy, asked
        #: for by name when a Eurostat table needs them, never inferred.
        _INFER_ORDER = ("GEM", "VWG", "KRS", "RBZ", "LAN", "STA")

        def _infer_level(output_path, cache_dir, match, bbox, land_only) -> dict:
            """Try levels smallest-first and return the first that matches.

            Why this exists: measured across all sessions, `geocode` was called 131
            times and `fetch_boundaries` 7. That gap is not ignorance of the tool —
            the prompt devotes 3.6k characters to it — but friction. The wrong route
            cost one word (`geocode("Tegernheim")`); the right one cost a lookup in
            a separate taxonomy (`boundaries_levels`, 2 calls ever) plus two
            required arguments. A rule cannot out-argue that gradient, so the
            gradient goes.
            """
            if not match:
                # Without a filter, GEM would download every German Gemeinde before
                # discovering it "matched". Refuse rather than infer.
                return {
                    "ok": False,
                    "error": "fetch_boundaries needs `match` (a name or key prefix) "
                             "when `level` is not given — otherwise there is nothing "
                             "to infer the level from.",
                }
            tried: list[str] = []
            for candidate in _INFER_ORDER:
                result = boundaries.fetch_boundaries(
                    candidate, output_path, cache_dir,
                    match=match, bbox=bbox, land_only=land_only,
                )
                if result.get("ok"):
                    result["level_inferred"] = True
                    if tried:
                        result["levels_tried"] = tried
                    return result
                # Only a genuine miss may advance to the next level. A download or
                # read failure must NOT: it is indistinguishable from "not at this
                # level" by outcome alone, and treating it as one silently answers
                # with the wrong unit. Observed while building this: a cold GEM
                # download failed, the loop moved on, and "Tegernheim" came back as
                # a Verwaltungsgemeinschaft — plausible, well-formed, wrong.
                error = str(result.get("error") or "")
                if "units matched" not in error:
                    result["levels_tried"] = [*tried, candidate]
                    result["error"] = (
                        f"level inference stopped at {candidate}: {error}. This is "
                        "not a 'no such unit' answer — retry, or name `level` "
                        "explicitly."
                    )
                    return result
                tried.append(candidate)
            return {
                "ok": False,
                "error": f"no administrative unit matched '{match}' at any level "
                         f"({', '.join(_INFER_ORDER)}). Below the Gemeinde — a "
                         "Stadtbezirk, Ortsteil or Quartier — the BKG dataset has "
                         "nothing; use geodata_search for the city's own portal.",
                "levels_tried": tried,
            }

        def fetch_boundaries(
            output_path: str,
            match: str | None = None,
            level: str | None = None,
            bbox: list[float] | None = None,
            land_only: bool = True,
        ) -> dict:
            """Fetch official administrative boundary polygons into a GeoPackage.

            **The authoritative source for a named administrative area** — prefer it
            over a `geocode` polygon, which comes from OpenStreetMap and carries no
            join key.

            Usually one argument is enough: `fetch_boundaries("tegernheim.gpkg",
            match="Tegernheim")`. ``level`` is optional and is worked out from the
            match, searching from the smallest unit upward (GEM → VWG → KRS → RBZ →
            LAN → STA); the level actually used comes back in the result. Name it
            explicitly (STA/LAN/RBZ/KRS/VWG/GEM with the AGS key, or
            NUTS1/NUTS2/NUTS3 with NUTS_CODE) when you need a *particular* level —
            e.g. the Landkreis Regensburg rather than the city of the same name, or
            a level to match a statistics table's granularity.

            ``match`` filters by key prefix ("09" = Bayern) or name substring;
            ``bbox`` = [west, south, east, north] in WGS84 windows the result.
            ``land_only`` (default) keeps the GF=4 land polygons, dropping
            water-body variants. The output (EPSG:25832) carries the join key so a
            statistics table joins straight onto it.
            """
            output_path = resolve_path(output_path, ws, write=True)
            cache_dir = str(resolve_path("_boundaries", ws))
            try:
                if level:
                    r = boundaries.fetch_boundaries(
                        level, output_path, cache_dir,
                        match=match, bbox=bbox, land_only=land_only,
                    )
                else:
                    r = _infer_level(output_path, cache_dir, match, bbox, land_only)
                    level = r.get("level") or ""
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path,
                    source="connector/bkg-vg",
                    tool="fetch_boundaries",
                    query={"level": r["level"], "match": match, "bbox": bbox},
                    crs=r.get("crs"),
                    licence=r.get("licence"),
                )
            return r

        def swiss_boundaries_levels() -> dict:
            """List the fetchable Swiss administrative levels and their join keys
            (LAND/KANTON/BEZIRK/GEMEINDE), from swissBOUNDARIES3D (swisstopo)."""
            return {
                "ok": True,
                "levels": swisstopo.swiss_boundary_levels(),
                "licence": swisstopo._SWISSTOPO_LICENCE,
                "note": "GEMEINDE keyed by bfs_nummer (the Swiss statistics key) "
                "and carries einwohnerzahl (population). CRS EPSG:2056 (LV95).",
            }

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
            output_path = resolve_path(output_path, ws, write=True)
            cache_dir = str(resolve_path("_boundaries", ws))
            try:
                r = swisstopo.fetch_swissboundaries3d(
                    level,
                    output_path,
                    cache_dir,
                    match=match,
                    bbox_wgs84=bbox,
                    canton=canton,
                    ch_only=ch_only,
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path,
                    source="connector/swisstopo",
                    tool="fetch_swiss_boundaries",
                    query={"level": r["level"], "match": match, "canton": canton, "bbox": bbox},
                    crs=r.get("crs"),
                    licence=r.get("licence"),
                )
                warning = _canton_confusion_warning(r.get("level"), match, canton, r.get("units"))
                if warning:
                    r = {**r, "warning": warning}
            return r

        def austria_boundaries_levels() -> dict:
            """List the fetchable Austrian administrative levels and their join key
            (GEM/BEZIRK/NUTS1/NUTS2/NUTS3), from STATISTIK AUSTRIA."""
            return {
                "ok": True,
                "levels": austria.austria_boundary_levels(),
                "licence": austria._LICENCE,
                "note": "Key column g_id (GKZ for GEM, hierarchical → prefix match "
                "selects a Bundesland/Bezirk). CRS EPSG:31287 (MGI/Austria Lambert).",
            }

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
            output_path = resolve_path(output_path, ws, write=True)
            cache_dir = str(resolve_path("_at_boundaries", ws))
            try:
                r = austria.fetch_austria_boundaries(
                    level, output_path, cache_dir, match=match, bbox_wgs84=bbox
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if r.get("ok"):
                provenance.write_meta(
                    output_path,
                    source="connector/statistik-austria",
                    tool="fetch_austria_boundaries",
                    query={"level": r["level"], "match": match, "bbox": bbox},
                    crs=r.get("crs"),
                    licence=r.get("licence"),
                )
            return r

        return FunctionToolset(
            tools=[
                boundaries_levels,
                fetch_boundaries,
                swiss_boundaries_levels,
                fetch_swiss_boundaries,
                austria_boundaries_levels,
                fetch_austria_boundaries,
            ]
        )
