"""GeoStatisticsCapability — statistical-data connectors (Phase 5.8).

GeoBenchX's largest class of tasks joins a statistical table to an admin geometry
to make a thematic (choropleth) map. This capability fetches the *numbers*; the
join to geometry is a normal QGIS step.

Only **credential-free** sources are wired here. The three GENESIS-2020 sources
(Destatis Regionalstatistik / GENESIS-Online / Zensus 2022) were removed: their
REST API requires a registered account (a 10-char user id or a 32-char token),
and gating a core workflow behind per-machine credentials proved impractical.
What remains needs no login:

- ``eurostat`` — Eurostat dissemination API (JSON-stat). EU-wide, NUTS 0–3. Always
  on. Coarser than Gemeinde level.
- ``wikidata`` — Wikidata SPARQL. Germany, per Gemeinde/Kreis: population (P1082)
  and area (P2046) carrying the AGS key (P439) — the credential-free replacement
  for Regionalstatistik's Gemeinde-level population, sourced from official
  statistics. Search resolves a region name to its AGS/Kreisschlüssel via the
  MWAPI full-text index; the table query returns one row per Gemeinde under that
  prefix.
- ``worldbank`` — World Bank Indicators API (v2). Global, per country: ~1500 World
  Development Indicators keyed on the ISO-3 country code. Search greps the WDI
  catalog for an indicator code; the table fetches the most-recent non-empty value
  per country (``mrnev=1``), dropping aggregate rows (World, income groups).

Never invent numbers: if an authoritative value cannot be obtained, report the
blocker instead of fabricating a plausible one.

The connector *delivers the table*; joining it to geometry is a normal QGIS step
(``native:joinattributestable`` on the NUTS/AGS key) — no bespoke join tool. Every
tool returns ``{"ok": false, "error": …}`` instead of raising, so a network hiccup
never crashes the loop.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import provenance
from chester.adminlevels import region_hierarchy as _region_hierarchy
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

_EUROSTAT_BASE = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
)
_EUROSTAT_TOC = "https://ec.europa.eu/eurostat/api/dissemination/catalogue/toc/txt"
_EUROSTAT_LICENCE = "© European Union, Eurostat (reuse permitted with attribution)"
_EUROSTAT_KEY_HINT = "geo = NUTS code (join to NUTS geometries, e.g. Eurostat/GISCO)"

_WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
_WIKIDATA_LICENCE = "Wikidata (CC0 1.0) — figures sourced from official statistics"
_WIKIDATA_KEY_HINT = "ags = Amtlicher Gemeindeschlüssel (join to Gemeinde geometries)"

_WORLDBANK_BASE = "https://api.worldbank.org/v2/"
_WORLDBANK_LICENCE = "© World Bank, World Development Indicators (CC BY 4.0)"
_WORLDBANK_KEY_HINT = (
    "iso3 = ISO-3166 alpha-3 country code (join to country geometries, e.g. "
    "Natural Earth / GISCO)"
)

_HEADERS = {"User-Agent": "Chester-Geo-AI/1.0 (+statistics connector)"}

_INSTRUCTIONS = """\
## Statistical connectors (official statistics → choropleth)

To make a thematic map you need *numbers* joined to *geometry*. These tools fetch
the numbers; the join is a normal QGIS step. One credential-free source:

- `stats_sources()` — which statistical sources are reachable right now.
- `stats_search(source, term)` — find a table/dataset code by keyword.
- `stats_table(source, code, output_path, ...)` — download a table as CSV into the
  cache. It carries the NUTS `geo` region key — join that to an admin-boundary
  layer with QGIS (`native:joinattributestable`), then symbolise as a choropleth.

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


# ── HTTP (httpx, not urllib: it ships certifi; the system store lacks ec.europa.eu) ──


def _http_get(url: str, timeout: int = 90) -> bytes:
    resp = httpx.get(url, headers=_HEADERS, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


# ── Eurostat dissemination API (JSON-stat; no auth) ──────────────────────────


def eurostat_search(term: str, limit: int = 15) -> list[dict]:
    """Grep the Eurostat table-of-contents (txt) for datasets matching ``term``."""
    raw = _http_get(_EUROSTAT_TOC).decode("utf-8", "replace")
    term_l = term.lower()
    out = []
    for line in raw.splitlines():
        # TOC fields are quote-wrapped and tab-separated; titles carry indentation.
        cols = [c.strip().strip('"').strip() for c in line.split("\t")]
        if len(cols) < 3:
            continue
        title, code, kind = cols[0], cols[1], cols[2].lower()
        if kind not in ("dataset", "table"):  # skip folders and the header row
            continue
        if term_l in title.lower() or term_l in code.lower():
            out.append({"code": code, "title": title, "type": kind})
        if len(out) >= limit:
            break
    return out


def jsonstat_to_dataframe(js: dict):
    """Flatten a JSON-stat 2.0 response into a tidy DataFrame (one row per value).

    Each dimension becomes a column (its category *code*, plus a ``<dim>_label``
    column when the source provides labels); the observation is the ``value``
    column. Sparse ``value`` maps (Eurostat omits missing cells) are handled.
    """
    import pandas as pd

    dims = js["id"]
    sizes = js["size"]
    dimension = js["dimension"]
    values = js["value"]

    # Per-dimension ordered category codes (index maps code -> position).
    codes_by_dim, labels_by_dim = [], []
    for d in dims:
        cat = dimension[d]["category"]
        index = cat["index"]
        if isinstance(index, dict):
            ordered = sorted(index, key=lambda k: index[k])
        else:  # already a list
            ordered = list(index)
        codes_by_dim.append(ordered)
        labels_by_dim.append(cat.get("label") or {})

    # Strides for the row-major flat index used by JSON-stat.
    strides = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]

    if isinstance(values, dict):
        items = ((int(k), v) for k, v in values.items())
    else:
        items = ((i, v) for i, v in enumerate(values) if v is not None)

    rows = []
    for flat, val in items:
        row = {}
        rem = flat
        for i, d in enumerate(dims):
            pos = rem // strides[i]
            rem = rem % strides[i]
            code = codes_by_dim[i][pos]
            row[d] = code
            label = labels_by_dim[i].get(code)
            if label and label != code:
                row[f"{d}_label"] = label
        row["value"] = val
        rows.append(row)
    return pd.DataFrame(rows)


def eurostat_table(code: str, filters: dict | None = None):
    """Fetch a Eurostat dataset as a tidy DataFrame (JSON-stat → DataFrame)."""
    params = {"format": "JSON", "lang": "EN"}
    if filters:
        params.update({k: str(v) for k, v in filters.items()})
    url = _EUROSTAT_BASE + urllib.parse.quote(code) + "?" + urllib.parse.urlencode(params)
    raw = _http_get(url)
    obj = json.loads(raw)
    if "value" not in obj:
        msg = obj.get("error") or obj.get("warning") or "no data returned"
        raise RuntimeError(f"Eurostat: {msg}")
    return jsonstat_to_dataframe(obj)


# ── Wikidata SPARQL (per-municipality figures; no auth) ──────────────────────

# EntitySearch (the full-text index) resolves a region name → its regional key
# fast; a raw label CONTAINS scan over every P439 entity times out. A Landkreis
# carries P440 (Kreisschlüssel, 5-digit); a Gemeinde carries P439 (AGS, 8-digit,
# prefixed by its Kreisschlüssel) — so one key serves as the AGS prefix for the
# table query.
_WIKIDATA_SEARCH = """\
PREFIX mwapi: <https://www.mediawiki.org/ontology#API/>
SELECT ?item ?code ?name ?typeLabel WHERE {
  SERVICE wikibase:mwapi {
    bd:serviceParam wikibase:api "EntitySearch" .
    bd:serviceParam wikibase:endpoint "www.wikidata.org" .
    bd:serviceParam mwapi:search %(term)s .
    bd:serviceParam mwapi:language "de" .
    ?item wikibase:apiOutputItem mwapi:item .
  }
  { ?item wdt:P440 ?code } UNION { ?item wdt:P439 ?code }
  OPTIONAL { ?item rdfs:label ?name . FILTER(LANG(?name) = "de") }
  OPTIONAL { ?item wdt:P31 ?t . ?t rdfs:label ?typeLabel . FILTER(LANG(?typeLabel) = "de") }
}
LIMIT %(limit)d"""

# One row per municipality whose AGS starts with the given prefix (a Kreis key →
# its Gemeinden; a full AGS → that one Gemeinde). SAMPLE collapses any duplicate
# truthy population/area statements to a single row per AGS.
_WIKIDATA_TABLE = """\
SELECT ?ags (SAMPLE(?name) AS ?nm) (SAMPLE(?pop) AS ?population) (SAMPLE(?a) AS ?area_km2) WHERE {
  ?item wdt:P439 ?ags .
  FILTER(STRSTARTS(?ags, %(prefix)s))
  OPTIONAL { ?item wdt:P1082 ?pop }
  OPTIONAL { ?item wdt:P2046 ?a }
  OPTIONAL { ?item rdfs:label ?name . FILTER(LANG(?name) = "de") }
}
GROUP BY ?ags
ORDER BY ?ags"""


def _sparql(query: str) -> list[dict]:
    """Run a SPARQL query against Wikidata; return simplified bindings (var → value)."""
    url = _WIKIDATA_ENDPOINT + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
    obj = json.loads(_http_get(url))
    return [
        {k: v.get("value") for k, v in binding.items()}
        for binding in obj.get("results", {}).get("bindings", [])
    ]


def wikidata_search(term: str, limit: int = 15) -> list[dict]:
    """Find German admin areas by name → their AGS/Kreisschlüssel (the table code)."""
    q = _WIKIDATA_SEARCH % {"term": json.dumps(term), "limit": int(limit)}
    seen: set[str] = set()
    out = []
    for r in _sparql(q):
        code = r.get("code")
        if code and code not in seen:
            seen.add(code)
            out.append({"code": code, "title": r.get("name"), "type": r.get("typeLabel")})
    return out


def wikidata_table(code: str):
    """Municipalities under an AGS prefix as a DataFrame (ags, name, population, area_km2)."""
    import pandas as pd

    rows = _sparql(_WIKIDATA_TABLE % {"prefix": json.dumps(str(code))})
    df = pd.DataFrame(rows).rename(columns={"nm": "name"})
    if df.empty:
        raise RuntimeError(
            f"no municipalities found for AGS prefix '{code}' — use stats_search "
            "to get a valid Kreis/Gemeinde code first"
        )
    cols = [c for c in ("ags", "name", "population", "area_km2") if c in df.columns]
    return df[cols]


# ── World Bank Indicators API (global, country level; no auth) ───────────────


def _worldbank_get(path: str, params: dict) -> tuple[dict, list]:
    """GET a World Bank v2 endpoint → (metadata, rows). Responses carry a BOM."""
    p = {"format": "json", **params}
    url = _WORLDBANK_BASE + path + "?" + urllib.parse.urlencode(p)
    obj = json.loads(_http_get(url).decode("utf-8-sig"))
    if not isinstance(obj, list) or len(obj) < 2:
        # WB signals an error as [{"message": [...]}] instead of [meta, rows].
        msg = obj[0].get("message") if isinstance(obj, list) and obj else obj
        raise RuntimeError(f"World Bank: {msg}")
    return obj[0], obj[1] or []


def worldbank_search(term: str, limit: int = 15) -> list[dict]:
    """Grep the World Development Indicators catalog (source 2) for ``term``."""
    _, inds = _worldbank_get("indicator", {"source": "2", "per_page": "3000"})
    tl = term.lower()
    out = []
    for i in inds:
        code, name = i.get("id") or "", i.get("name") or ""
        if tl in name.lower() or tl in code.lower():
            out.append({"code": code, "title": name})
        if len(out) >= limit:
            break
    return out


def _worldbank_aggregate_iso3() -> set[str]:
    """ISO-3 codes that are *aggregates* (World, EU, income groups), to drop them.

    Aggregates carry ``region.value == "Aggregates"`` in the country metadata; real
    countries carry a real region — so a country choropleth keeps only the latter.
    """
    _, countries = _worldbank_get("country", {"per_page": "400"})
    return {
        c["id"] for c in countries
        if (c.get("region") or {}).get("value") == "Aggregates"
    }


def worldbank_table(code: str, filters: dict | None = None):
    """One indicator as a DataFrame keyed by ISO-3 (iso3, country, year, value).

    Default is the most-recent non-empty value per country (``mrnev=1``); pass
    ``filters={"date": "2020"}`` for a specific year. Aggregate rows are dropped.
    """
    import pandas as pd

    params: dict = {"per_page": "400"}
    if filters:
        params.update({k: str(v) for k, v in filters.items()})
    else:
        params["mrnev"] = "1"
    _, rows = _worldbank_get(
        "country/all/indicator/" + urllib.parse.quote(code), params
    )
    if not rows:
        raise RuntimeError(f"no data for indicator '{code}' (check the code)")
    aggregates = _worldbank_aggregate_iso3()
    recs = [
        {
            "iso3": r.get("countryiso3code"),
            "country": (r.get("country") or {}).get("value"),
            "year": r.get("date"),
            "value": r.get("value"),
        }
        for r in rows
        if r.get("countryiso3code") and r["countryiso3code"] not in aggregates
    ]
    df = pd.DataFrame(recs)
    if df.empty:
        raise RuntimeError(f"indicator '{code}' returned no country-level values")
    return df


# ── capability ───────────────────────────────────────────────────────────────


@dataclass
class GeoStatisticsCapability(AbstractCapability[Any]):
    """Fetch official statistics (Eurostat; credential-free)."""

    workspace: str = DEFAULT_WORKSPACE
    # Reserved for future authenticated sources; unused by the credential-free
    # Eurostat connector. Kept so agent_build can keep threading geodata.statistics.
    statistics: dict = field(default_factory=dict)

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def _source_status(self) -> list[dict]:
        """Each source with its area, level and whether it is reachable now."""
        return [
            {"source": "eurostat", "api": "json-stat", "area": "EU",
             "configured": True, "key": _EUROSTAT_KEY_HINT},
            {"source": "wikidata", "api": "sparql", "area": "DE (Gemeinde/Kreis)",
             "configured": True, "key": _WIKIDATA_KEY_HINT},
            {"source": "worldbank", "api": "worldbank-v2", "area": "Global (country)",
             "configured": True, "key": _WORLDBANK_KEY_HINT},
        ]

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace

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
            return {"ok": True, "sources": self._source_status()}

        def stats_search(source: str, term: str, limit: int = 15) -> dict:
            """Find statistical tables/datasets by keyword in a given source.

            ``source`` is `eurostat`, `wikidata` or `worldbank`. Returns candidate
            `code`s to pass to `stats_table`. For `wikidata`, search a region name
            (e.g. "Landkreis Regensburg") to get its AGS/Kreisschlüssel code; for
            `worldbank`, search an indicator (e.g. "population" → SP.POP.TOTL).
            """
            try:
                if source == "eurostat":
                    results = eurostat_search(term, limit=limit)
                elif source == "wikidata":
                    results = wikidata_search(term, limit=limit)
                elif source == "worldbank":
                    results = worldbank_search(term, limit=limit)
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
            output_path = resolve_path(output_path, ws)
            if not output_path.lower().endswith(".csv"):
                output_path += ".csv"
            try:
                if source == "eurostat":
                    df = eurostat_table(code, filters=filters)
                    df.to_csv(output_path, index=False)
                    licence, key = _EUROSTAT_LICENCE, _EUROSTAT_KEY_HINT
                    preview = _preview(df)
                elif source == "wikidata":
                    df = wikidata_table(code)
                    df.to_csv(output_path, index=False)
                    licence, key = _WIKIDATA_LICENCE, _WIKIDATA_KEY_HINT
                    preview = _preview(df)
                elif source == "worldbank":
                    df = worldbank_table(code, filters=filters)
                    df.to_csv(output_path, index=False)
                    licence, key = _WORLDBANK_LICENCE, _WORLDBANK_KEY_HINT
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

        return FunctionToolset(
            tools=[stats_sources, stats_search, stats_table, region_hierarchy]
        )
