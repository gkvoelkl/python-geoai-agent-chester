"""The three statistics sources — Eurostat, Wikidata, World Bank.

A **pure core** beside `geoops`/`rasterops`: fetches tables, translates JSON-stat and
SPARQL into DataFrames, knows licences and key columns. No tools, no framework — the
wrappers live in `chester/statisticstools.py`.

Split out on 2026-09-14 because the wrapper module would otherwise have had 448 lines.
The cut also gets the layering right: source access is core, not wrapper.
"""

from __future__ import annotations

import json
import urllib.parse

import httpx

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


def _http_get(url: str, timeout: int = 90) -> bytes:
    resp = httpx.get(url, headers=_HEADERS, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


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


def _source_status() -> list[dict]:
    """Each source with its area, level and whether it is reachable now."""
    return [
        {"source": "eurostat", "api": "json-stat", "area": "EU",
         "configured": True, "key": _EUROSTAT_KEY_HINT},
        {"source": "wikidata", "api": "sparql", "area": "DE (Gemeinde/Kreis)",
         "configured": True, "key": _WIKIDATA_KEY_HINT},
        {"source": "worldbank", "api": "worldbank-v2", "area": "Global (country)",
         "configured": True, "key": _WORLDBANK_KEY_HINT},
    ]
