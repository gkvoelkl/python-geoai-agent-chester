"""Statistical-connector tests (offline: JSON-stat parsing + Eurostat contract).

The live Eurostat calls need network; those are exercised in the opt-in network
suite. Here we cover the pure logic: JSON-stat flattening and the eurostat-only
source contract (the credential-gated German GENESIS sources were removed).
"""

from __future__ import annotations

from _util import tools_of

from chester import provenance
from chester.capabilities.statistics import (
    GeoStatisticsCapability,
    jsonstat_to_dataframe,
)

# ── JSON-stat parsing ────────────────────────────────────────────────────────


def _mini_jsonstat() -> dict:
    """A 2×2 JSON-stat cube (geo × time), one cell missing (sparse value map)."""
    return {
        "id": ["geo", "time"],
        "size": [2, 2],
        "dimension": {
            "geo": {"category": {
                "index": {"DE": 0, "FR": 1},
                "label": {"DE": "Germany", "FR": "France"},
            }},
            "time": {"category": {"index": {"2021": 0, "2022": 1}}},
        },
        # row-major: (DE,2021)=0, (DE,2022)=1, (FR,2021)=2, (FR,2022)=3
        "value": {"0": 10.0, "1": 11.0, "3": 13.0},  # (FR,2021) omitted
    }


def test_jsonstat_flattens_to_tidy_rows():
    df = jsonstat_to_dataframe(_mini_jsonstat())
    assert len(df) == 3  # the sparse (FR,2021) cell is dropped
    assert set(df.columns) >= {"geo", "time", "value", "geo_label"}
    de22 = df[(df["geo"] == "DE") & (df["time"] == "2022")]
    assert de22["value"].iloc[0] == 11.0
    assert de22["geo_label"].iloc[0] == "Germany"
    # time has no labels → no time_label column
    assert "time_label" not in df.columns


def test_jsonstat_handles_dense_value_list():
    js = _mini_jsonstat()
    js["value"] = [10.0, 11.0, 12.0, 13.0]  # dense list form
    df = jsonstat_to_dataframe(js)
    assert len(df) == 4
    fr21 = df[(df["geo"] == "FR") & (df["time"] == "2021")]
    assert fr21["value"].iloc[0] == 12.0


# ── source contract (eurostat only; credential sources removed) ──────────────


def test_sources_lists_only_credential_free_sources():
    tools = tools_of(GeoStatisticsCapability(statistics={}))
    r = tools["stats_sources"]()
    assert r["ok"]
    by = {s["source"]: s for s in r["sources"]}
    # all wired sources are credential-free
    assert by["eurostat"]["configured"] is True
    assert by["wikidata"]["configured"] is True
    assert by["wikidata"]["key"].startswith("ags")
    assert by["worldbank"]["configured"] is True
    assert by["worldbank"]["key"].startswith("iso3")
    # the credential-gated German GENESIS sources are gone
    assert "genesis" not in by
    assert "regionalstatistik" not in by
    assert "zensus2022" not in by


def test_removed_german_source_is_now_unknown():
    tools = tools_of(GeoStatisticsCapability(statistics={}))
    r = tools["stats_search"](source="regionalstatistik", term="Bevölkerung")
    assert r["ok"] is False and "unknown source" in r["error"]
    r2 = tools["stats_table"](source="genesis", code="12345", output_path="x")
    assert r2["ok"] is False and "unknown source" in r2["error"]


def test_unknown_source_errors():
    tools = tools_of(GeoStatisticsCapability(statistics={}))
    r = tools["stats_search"](source="bogus", term="x")
    assert r["ok"] is False and "unknown source" in r["error"]


def test_eurostat_table_writes_csv_and_sidecar(tmp_path, monkeypatch):
    """stats_table('eurostat', ...) writes a CSV + provenance without real network."""
    import chester.capabilities.statistics as st

    monkeypatch.setattr(st, "eurostat_table",
                        lambda code, filters=None: jsonstat_to_dataframe(_mini_jsonstat()))
    tools = tools_of(GeoStatisticsCapability(workspace=str(tmp_path), statistics={}))
    r = tools["stats_table"](source="eurostat", code="demo_pop", output_path="pop")
    assert r["ok"] and r["output"].endswith(".csv")
    assert r["join_key"].startswith("geo")
    meta = provenance.read_meta(r["output"])
    assert meta and meta["source"] == "connector/eurostat"


# ── Wikidata connector (SPARQL parsing + eurostat-shaped contract, mocked) ───


def _fake_sparql_bindings() -> list[dict]:
    """What _sparql returns for a table query: one flattened row per municipality."""
    return [
        {"ags": "09375113", "name": "Alteglofsheim", "population": "3360", "area_km2": "13.22"},
        {"ags": "09375114", "name": "Altenthann", "population": "1514", "area_km2": "21.48"},
    ]


def test_wikidata_table_shapes_dataframe(monkeypatch):
    import chester.capabilities.statistics as st

    monkeypatch.setattr(st, "_sparql", lambda q: _fake_sparql_bindings())
    df = st.wikidata_table("09375")
    assert list(df.columns) == ["ags", "name", "population", "area_km2"]
    assert len(df) == 2 and df.iloc[0]["ags"] == "09375113"


def test_wikidata_table_empty_prefix_errors(monkeypatch):
    import chester.capabilities.statistics as st

    monkeypatch.setattr(st, "_sparql", lambda q: [])
    tools = tools_of(GeoStatisticsCapability(statistics={}))
    r = tools["stats_table"](source="wikidata", code="99999", output_path="x")
    assert r["ok"] is False and "no municipalities" in r["error"]


def test_wikidata_table_writes_csv_and_sidecar(tmp_path, monkeypatch):
    import chester.capabilities.statistics as st

    monkeypatch.setattr(st, "_sparql", lambda q: _fake_sparql_bindings())
    tools = tools_of(GeoStatisticsCapability(workspace=str(tmp_path), statistics={}))
    r = tools["stats_table"](source="wikidata", code="09375", output_path="pop")
    assert r["ok"] and r["output"].endswith(".csv")
    assert r["join_key"].startswith("ags")
    assert "population" in r["columns"]
    meta = provenance.read_meta(r["output"])
    assert meta and meta["source"] == "connector/wikidata"


# ── World Bank connector (catalog grep + ISO join, aggregates dropped) ───────


def _fake_worldbank_get(path, params):
    """Dispatch the two/three endpoints worldbank_search/table hit, offline."""
    if path.startswith("country/all/indicator"):
        return {}, [
            {"countryiso3code": "DEU", "country": {"id": "DE", "value": "Germany"},
             "date": "2023", "value": 83000000},
            {"countryiso3code": "WLD", "country": {"id": "1W", "value": "World"},
             "date": "2023", "value": 8000000000},          # aggregate → dropped
            {"countryiso3code": "", "country": {"id": "XX", "value": "?"},
             "date": "2023", "value": 1},                    # blank iso3 → dropped
        ]
    if path == "country":
        return {}, [
            {"id": "DEU", "region": {"value": "Europe & Central Asia"}},
            {"id": "WLD", "region": {"value": "Aggregates"}},
        ]
    if path == "indicator":
        return {}, [
            {"id": "SP.POP.TOTL", "name": "Population, total"},
            {"id": "NY.GDP.MKTP.CD", "name": "GDP (current US$)"},
        ]
    raise AssertionError(f"unexpected WB path: {path}")


def test_worldbank_search_greps_catalog(monkeypatch):
    import chester.capabilities.statistics as st

    monkeypatch.setattr(st, "_worldbank_get", _fake_worldbank_get)
    assert st.worldbank_search("population") == [
        {"code": "SP.POP.TOTL", "title": "Population, total"}
    ]


def test_worldbank_table_drops_aggregates_and_keys_on_iso3(monkeypatch):
    import chester.capabilities.statistics as st

    monkeypatch.setattr(st, "_worldbank_get", _fake_worldbank_get)
    df = st.worldbank_table("SP.POP.TOTL")
    assert list(df.columns) == ["iso3", "country", "year", "value"]
    assert list(df["iso3"]) == ["DEU"]  # WLD (aggregate) and blank dropped


def test_worldbank_table_writes_csv_and_sidecar(tmp_path, monkeypatch):
    import chester.capabilities.statistics as st

    monkeypatch.setattr(st, "_worldbank_get", _fake_worldbank_get)
    tools = tools_of(GeoStatisticsCapability(workspace=str(tmp_path), statistics={}))
    r = tools["stats_table"](source="worldbank", code="SP.POP.TOTL", output_path="pop")
    assert r["ok"] and r["join_key"].startswith("iso3")
    meta = provenance.read_meta(r["output"])
    assert meta and meta["source"] == "connector/worldbank"
