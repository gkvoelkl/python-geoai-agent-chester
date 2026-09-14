"""GeoStatisticsCapability — statistical-data connectors (Phase 5.8).

Rahmenneutrale Hüllen (Phase KM, Schritt 1): Werkzeuge einmal beschrieben,
zwei Adapter — `capabilities/statistics.py` für Chesters Agenten, später der
MCP-Server. Kein `pydantic_ai`, kein `selmakit`.
"""

from __future__ import annotations

from collections.abc import Callable

# Modul statt Namen: So greift ein Patch am Kern auch hier — die Tests
# ersetzen dort `_sparql`/`_worldbank_get`, und ein früh gebundener Name
# hätte das stillschweigend ins Leere laufen lassen.
from chester import provenance, statsources
from chester.adminlevels import region_hierarchy as _region_hierarchy
from chester.workspace import resolve_path

INSTRUCTIONS = """\
## Statistical connectors (official statistics → choropleth)

To make a thematic map you need *numbers* joined to *geometry*. These tools fetch
the numbers; the join is a normal QGIS step. One credential-free source:

- `stats_sources()` — which statistical sources are reachable right now.
- `stats_search(source, term)` — find a table/dataset code by keyword.
- `stats_table(source, code, output_path, ...)` — download a table as CSV into the
  cache. It carries the NUTS `geo` region key — join that to an admin-boundary
  layer with `vector_join`, then symbolise as a choropleth. Check its `unjoined`
  count first: a key read as a number has lost its leading zero, and a choropleth
  over an empty column still draws.

Sources (all credential-free):
- `eurostat` — EU-wide, NUTS 0–3. Use for cross-country/region EU comparison.
- `wikidata` — Germany, per Gemeinde/Kreis. Carries the AGS join key plus
  `population` (P1082) and `area_km2` (P2046) — the source for a German
  municipality choropleth. Two steps: `stats_search("wikidata", "Landkreis
  Regensburg")` → its code "09375"; then `stats_table("wikidata", "09375", …)` →
  a CSV of every Gemeinde in that Kreis with ags/population/area_km2.
- `worldbank` — global, per country (ISO-3 key). ~1500 development indicators
  (population, GDP, health, environment). `stats_search("worldbank", "population")`
  → an indicator code (e.g. "SP.POP.TOTL"); `stats_table("worldbank", "SP.POP.TOTL",
  …)` → a CSV of iso3/value (latest year). Use for a world/country choropleth.

A statistical table has no geometry — always report the join key you used and
validate the join count before mapping.

If per-unit data isn't found, **escalate the scope**: region keys encode the
hierarchy as a prefix, so a Gemeinde that yields nothing may be present in the
whole-Kreis, whole-Land or whole-Bund dataset. Call `region_hierarchy(code)` for
the wider prefixes and fetch the comprehensive set filtered to your unit(s) — e.g.
`stats_table("wikidata", "09")` is every Bavarian Gemeinde. Escalate the *scope*,
keep the *granularity*: if you cannot obtain a value at the needed granularity from
an authoritative source, say so and report the blocker — never fabricate numbers
and never pass off a higher-level aggregate as a missing unit's value.\
"""






























def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """Die Werkzeuge dieser Gruppe, an ``workspace`` gebunden."""
    ws = workspace

    def _preview(df) -> dict:
        head = df.head(5).to_dict(orient="records")
        return {"rows": int(len(df)), "columns": list(df.columns), "head": head}

    def region_hierarchy(code: str) -> dict:
        """The administrative-escalation chain for an AGS/Kreisschlüssel or NUTS
        code — the wider scopes to try when per-unit data isn't found.

        Region keys encode containment as a prefix, so escalating to the next
        level is shortening the prefix: "09375117" (Gemeinde) → "09375" (Kreis)
        → "09" (Land/Bayern) → "" (Bund). Each returned scope carries the
        ``prefix`` to fetch the comprehensive dataset for that level and filter
        to your unit(s) — e.g. `stats_table("wikidata", "09")` for every
        Bavarian Gemeinde. Escalate the search scope, not the granularity: never
        report a higher-level aggregate as a missing unit's value.
        """
        return _region_hierarchy(code)

    def stats_sources() -> dict:
        """List the statistical sources and whether each is reachable now.

        Only the credential-free `eurostat` source is wired (the German
        GENESIS sources were removed — they required an account). Each entry
        names the join key its tables carry.
        """
        return {"ok": True, "sources": statsources._source_status()}

    def stats_search(source: str, term: str, limit: int = 15) -> dict:
        """Find statistical tables/datasets by keyword in a given source.

        ``source`` is `eurostat`, `wikidata` or `worldbank`. Returns candidate
        `code`s to pass to `stats_table`. For `wikidata`, search a region name
        (e.g. "Landkreis Regensburg") to get its AGS/Kreisschlüssel code; for
        `worldbank`, search an indicator (e.g. "population" → SP.POP.TOTL).
        """
        try:
            if source == "eurostat":
                results = statsources.eurostat_search(term, limit=limit)
            elif source == "wikidata":
                results = statsources.wikidata_search(term, limit=limit)
            elif source == "worldbank":
                results = statsources.worldbank_search(term, limit=limit)
            else:
                return {"ok": False, "error": f"unknown source '{source}' "
                        "(available: 'eurostat', 'wikidata', 'worldbank')"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "source": source, "count": len(results),
                "results": results}

    def stats_table(
        source: str,
        code: str,
        output_path: str,
        filters: dict | None = None,
    ) -> dict:
        """Download one statistical table as a CSV into the cache.

        ``source`` is `eurostat`, `wikidata` or `worldbank`; ``code`` comes
        from `stats_search`. For `eurostat`, ``code`` is a dataset code and
        ``filters`` narrows it (e.g. ``{"geoLevel": "nuts2", "time": "2022"}``);
        the CSV carries the NUTS ``geo`` key. For `wikidata`, ``code`` is an
        AGS/Kreisschlüssel prefix (e.g. "09375") and the CSV carries per-
        Gemeinde ``ags`` + ``population`` + ``area_km2``. For `worldbank`,
        ``code`` is an indicator (e.g. "SP.POP.TOTL"), the CSV carries per-
        country ``iso3`` + ``value`` (latest year, or ``filters={"date":
        "2020"}``). Join the key column to an admin-boundary layer with QGIS,
        then map.
        """
        output_path = resolve_path(output_path, ws, write=True)
        if not output_path.lower().endswith(".csv"):
            output_path += ".csv"
        try:
            if source == "eurostat":
                df = statsources.eurostat_table(code, filters=filters)
                df.to_csv(output_path, index=False)
                licence, key = statsources._EUROSTAT_LICENCE, statsources._EUROSTAT_KEY_HINT
                preview = _preview(df)
            elif source == "wikidata":
                df = statsources.wikidata_table(code)
                df.to_csv(output_path, index=False)
                licence, key = statsources._WIKIDATA_LICENCE, statsources._WIKIDATA_KEY_HINT
                preview = _preview(df)
            elif source == "worldbank":
                df = statsources.worldbank_table(code, filters=filters)
                df.to_csv(output_path, index=False)
                licence, key = statsources._WORLDBANK_LICENCE, statsources._WORLDBANK_KEY_HINT
                preview = _preview(df)
            else:
                return {"ok": False, "error": f"unknown source '{source}' "
                        "(available: 'eurostat', 'wikidata', 'worldbank')"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        provenance.write_meta(
            output_path, source=f"connector/{source}", tool="stats_table",
            query={"source": source, "code": code, "filters": filters},
            licence=licence,
        )
        return {"ok": True, "source": source, "code": code,
                "output": output_path, "join_key": key, **preview}

    return [stats_sources, stats_search, stats_table, region_hierarchy]
