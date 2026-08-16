"""V1 — richer level-1 validation checks (doc/validation-concept.md Ebene 1).

Covers ``geofacts.attribute_facts`` (null/placeholder/out-of-range per field), the
``chester.plausibility`` domain bands, the ``sanity_check_result`` attribute
expectations, and the gate's placeholder-saturation detection. Offline, no QGIS.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from _util import tools_of
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from chester import geofacts, plausibility
from chester.capabilities.validation import GeoValidationCapability
from chester.gate import make_validation_gate
from chester.geofacts import attribute_facts


def _write(path: Path, data: dict, crs: str = "EPSG:25832") -> Path:
    import geopandas as gpd
    from shapely.geometry import Point

    n = len(next(iter(data.values())))
    gdf = gpd.GeoDataFrame(
        data, geometry=[Point(11.0 + i * 1e-4, 49.0) for i in range(n)], crs=crs
    )
    gdf.to_file(path, driver="GPKG")
    return path


# ── attribute_facts ──────────────────────────────────────────────────────────


def test_attribute_facts_counts_null_placeholder_range(tmp_path):
    p = _write(
        tmp_path / "a.gpkg",
        {"h": [10.0, -9999.0, None, 500.0], "name": ["x", "NULL", "y", "z"]},
    )
    af = attribute_facts(p, ranges={"h": (0.0, 200.0)})
    fh = af["fields"]["h"]
    assert fh["null"] == 1
    assert fh["placeholder"] == 1          # the -9999
    assert fh["out_of_range"] == 2         # -9999 and 500 are outside [0, 200]
    assert not fh["all_placeholder"]
    assert af["fields"]["name"]["placeholder"] == 1  # "NULL"
    assert af["row_count"] == 4


def test_attribute_facts_all_placeholder_and_required(tmp_path):
    p = _write(tmp_path / "b.gpkg", {"val": [-9999.0, -9999.0, -9999.0]})
    af = attribute_facts(p, required=["val", "absent"])
    assert af["fields"]["val"]["all_placeholder"] is True
    # 'val' is effectively empty (all sentinel) and 'absent' is missing
    assert set(af["missing_required"]) == {"val", "absent"}


def test_attribute_facts_empty_string_is_placeholder_by_default(tmp_path):
    p = _write(tmp_path / "c.gpkg", {"s": ["", "", ""]})
    af = attribute_facts(p)
    assert af["fields"]["s"]["all_placeholder"] is True
    # but with a stricter set (the gate's) the empty string is not a placeholder
    af2 = attribute_facts(p, placeholder_strings={"null", "nan"})
    assert af2["fields"]["s"]["all_placeholder"] is False


# The three tests above cover the happy paths. Mutation testing (H4) showed the
# gate-critical details were unasserted: the sentinel *sets*, case folding, the
# `populated` field, and the `>=` that decides `all_placeholder`. A wrong count here
# means the gate misses a failed join — the exact failure V1 exists to catch.


def test_placeholder_sets_are_pinned():
    """The sentinel values themselves.

    `-9999` is the classic leaked nodata; `-99999` appears in some state DEMs. If a
    value silently left this set, a column full of it would read as healthy data.
    """
    assert geofacts.DEFAULT_PLACEHOLDER_NUMBERS == {-9999.0, -99999.0}
    assert geofacts.DEFAULT_PLACEHOLDER_STRINGS == {
        "", "null", "none", "nan", "n/a", "#n/a"}


def test_every_default_string_placeholder_is_recognised(tmp_path):
    # One row per sentinel spelling, in mixed case and with padding — the reader
    # strips and lowercases, and that normalisation must not quietly disappear.
    values = ["NULL", " none ", "NaN", "N/A", "#N/A", ""]
    p = _write(tmp_path / "ph.gpkg", {"s": values})
    af = attribute_facts(p)
    assert af["fields"]["s"]["placeholder"] == len(values)
    assert af["fields"]["s"]["all_placeholder"] is True


def test_second_numeric_sentinel_is_recognised(tmp_path):
    p = _write(tmp_path / "ph2.gpkg", {"v": [-99999.0, -99999.0, 5.0]})
    af = attribute_facts(p)
    assert af["fields"]["v"]["placeholder"] == 2
    assert af["fields"]["v"]["all_placeholder"] is False


def test_populated_excludes_nulls_and_counts_accumulate(tmp_path):
    p = _write(tmp_path / "pop.gpkg",
               {"v": [1.0, None, None, -9999.0, -9999.0, -9999.0]})
    fv = attribute_facts(p)["fields"]["v"]
    assert fv["null"] == 2
    assert fv["populated"] == 4           # 6 rows minus 2 nulls
    assert fv["placeholder"] == 3         # counts must accumulate past one
    assert fv["all_placeholder"] is False  # 3 sentinels < 4 populated


def test_all_placeholder_needs_every_populated_value(tmp_path):
    """The boundary that `>=` decides: one real value is enough to clear the flag."""
    almost = _write(tmp_path / "n1.gpkg", {"v": [-9999.0, -9999.0, 7.0]})
    assert attribute_facts(almost)["fields"]["v"]["all_placeholder"] is False
    exactly = _write(tmp_path / "n2.gpkg", {"v": [-9999.0, -9999.0, None]})
    fv = attribute_facts(exactly)["fields"]["v"]
    assert fv["populated"] == 2 and fv["placeholder"] == 2
    assert fv["all_placeholder"] is True


def test_an_empty_column_is_not_all_placeholder(tmp_path):
    # `populated > 0` guards this: nothing populated is "no data", not "all sentinel".
    p = _write(tmp_path / "empty.gpkg", {"v": [None, None]})
    fv = attribute_facts(p)["fields"]["v"]
    assert fv["populated"] == 0 and fv["all_placeholder"] is False


def test_healthy_required_field_is_not_reported_missing(tmp_path):
    p = _write(tmp_path / "ok.gpkg", {"h": [3.0, 4.0], "n": ["a", "b"]})
    assert attribute_facts(p, required=["h", "n"])["missing_required"] == []


def test_out_of_range_bounds_are_inclusive(tmp_path):
    p = _write(tmp_path / "r.gpkg", {"h": [0.0, 200.0, -0.001, 200.001]})
    fh = attribute_facts(p, ranges={"h": (0.0, 200.0)})["fields"]["h"]
    assert fh["out_of_range"] == 2, "genau auf der Grenze zählt nicht als außerhalb"


def test_vector_facts_reports_crs_kind_and_count(tmp_path):
    p = _write(tmp_path / "v.gpkg", {"a": [1, 2, 3]})
    vf = geofacts.vector_facts(str(p))
    assert vf["kind"] == "vector"
    assert vf["feature_count"] == 3
    assert vf["is_geographic"] is False          # written as EPSG:25832
    assert "25832" in vf["crs"]
    assert vf["geometry_types"] == ["Point"]
    assert len(vf["bounds"]) == 4


def test_vector_facts_flags_a_geographic_crs(tmp_path):
    # The distinction the CRS check hangs on: measuring on degrees is the classic
    # error, so `is_geographic` must not silently invert.
    p = _write(tmp_path / "wgs.gpkg", {"a": [1]}, crs="EPSG:4326")
    assert geofacts.vector_facts(str(p))["is_geographic"] is True


# ── plausibility bands ───────────────────────────────────────────────────────


def test_plausibility_check_value():
    assert plausibility.check_value("building_height", 12) is None
    assert "below" in plausibility.check_value("building_height", 0.2)
    assert "above" in plausibility.check_value("building_height", 5000)
    assert plausibility.check_value("unknown_magnitude", 3) is None
    assert plausibility.check_value("building_height", "n/a") is None


def test_plausibility_check_series_counts():
    summ = plausibility.check_series("building_height", [10, 0.5, 300, "x", 20])
    assert summ["checked"] == 4          # "x" skipped
    assert summ["below"] == 1 and summ["above"] == 1
    assert summ["out_of_band"] == 2
    assert plausibility.check_series("nope", [1, 2]) is None


# The tests above probe far outside the bands (0.2, 5000) — which means a band edge
# could move by 50 % without any of them noticing. Mutation testing measured exactly
# that: 0 of 69 mutants killed. The four below pin the numbers, the edges, the
# accessor and the full return shape.


@pytest.mark.parametrize(("magnitude", "expected"), [
    ("building_height", (1.0, 200.0, "m")),
    ("building_area", (4.0, 200_000.0, "m2")),
    ("population", (0.0, 40_000_000.0, "count")),
    ("population_density", (0.0, 50_000.0, "per_km2")),
    ("slope", (0.0, 90.0, "degree")),
    ("elevation", (-500.0, 9_000.0, "m")),
    ("area_m2", (0.0, 1e12, "m2")),
    ("length_m", (0.0, 1e8, "m")),
])
def test_band_values_are_pinned(magnitude, expected):
    """The numbers themselves, not just "some band exists".

    These bounds are the deterministic floor `sanity_check_result` and the gate rely
    on; a silent edit here would quietly change what counts as absurd.
    """
    assert plausibility.band(magnitude) == expected
    assert plausibility.BANDS[magnitude] == expected


def test_band_returns_none_for_an_unknown_magnitude():
    assert plausibility.band("no_such_magnitude") is None


@pytest.mark.parametrize("magnitude", sorted(plausibility.BANDS))
def test_band_edges_are_inclusive(magnitude):
    """Exactly on the edge is plausible; a hair outside is not.

    This is what distinguishes `<` from `<=` — the comparison a mutant flips first.
    """
    lo, hi, _unit = plausibility.BANDS[magnitude]
    assert plausibility.check_value(magnitude, lo) is None
    assert plausibility.check_value(magnitude, hi) is None
    span = hi - lo
    assert "below" in plausibility.check_value(magnitude, lo - span * 1e-6)
    assert "above" in plausibility.check_value(magnitude, hi + span * 1e-6)


def test_check_value_message_names_the_range_and_unit():
    msg = plausibility.check_value("slope", 91)
    assert "91" in msg and "degree" in msg and "[0.0, 90.0]" in msg
    assert "slope" in msg


def test_check_series_reports_the_band_it_used():
    # The caller needs to know *which* band produced the counts, not just the counts.
    summ = plausibility.check_series("slope", [0, 90, 91, -1, "x"])
    assert summ == {"magnitude": "slope", "unit": "degree", "min": 0.0, "max": 90.0,
                    "checked": 4, "below": 1, "above": 1, "out_of_band": 2}


def test_check_series_counts_accumulate_beyond_one():
    """Several violations must count as several, not as one.

    Found by mutation testing: `below += 1` → `below = 1` survived, because every
    earlier case had exactly one outlier per direction. A column with five absurd
    heights would have reported `below: 1` — and a caller weighing "how bad is it"
    would have been told the wrong thing.
    """
    summ = plausibility.check_series(
        "building_height", [0.1, 0.2, 0.3, 500.0, 600.0, 12.0])
    assert summ["below"] == 3
    assert summ["above"] == 2
    assert summ["out_of_band"] == 5
    assert summ["checked"] == 6


def test_check_series_on_an_all_valid_column_reports_zero():
    summ = plausibility.check_series("building_height", [3.0, 12.5, 199.9])
    assert summ["checked"] == 3 and summ["out_of_band"] == 0
    assert summ["below"] == 0 and summ["above"] == 0


# ── sanity_check_result attribute expectations ───────────────────────────────


def _sanity(tmp_path):
    return tools_of(GeoValidationCapability(workspace=str(tmp_path)))["sanity_check_result"]


def test_sanity_warns_on_placeholder_saturated_column(tmp_path):
    sanity = _sanity(tmp_path)
    p = _write(tmp_path / "s.gpkg", {"pop": [-9999.0, -9999.0]})
    r = sanity(str(p))
    assert r["ok"] is False
    assert any("placeholder/sentinel" in w for w in r["warnings"])


def test_sanity_warns_on_range_and_required_and_magnitude(tmp_path):
    sanity = _sanity(tmp_path)
    p = _write(tmp_path / "s2.gpkg", {"height": [12.0, 5000.0]})
    r = sanity(
        str(p),
        ranges={"height": [0, 200]},
        required=["missing_col"],
        magnitude_field="height",
        magnitude="building_height",
    )
    warns = " ".join(r["warnings"])
    assert "outside range" in warns
    assert "required field 'missing_col'" in warns
    assert "building_height" in warns


def test_sanity_clean_result_has_no_attribute_warnings(tmp_path):
    sanity = _sanity(tmp_path)
    p = _write(tmp_path / "ok.gpkg", {"height": [12.0, 15.0]})
    r = sanity(str(p), magnitude_field="height", magnitude="building_height")
    assert r["ok"] is True
    assert r["warnings"] == []


# ── gate consumption (strict placeholder saturation → retry) ─────────────────


def _ctx(tool_output):
    part = ToolReturnPart(tool_name="join", content=tool_output, tool_call_id="c1")
    req = ModelRequest(parts=[part])
    run_id = None
    try:
        req.run_id = "R1"
        run_id = "R1"
    except Exception:  # noqa: BLE001
        pass
    return SimpleNamespace(deps="s1", messages=[req], run_id=run_id, retry=0, max_retries=1)


def test_gate_retries_on_all_sentinel_column(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "geocache").mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    gate = make_validation_gate(sessions_dir=str(sessions), workspace=str(ws))
    p = _write(ws / "geocache" / "choropleth.gpkg", {"pop": [-9999.0, -9999.0]})

    async def go():
        try:
            await gate(_ctx({"ok": True, "output": str(p)}), "Result in choropleth.gpkg.")
            return "PASS"
        except ModelRetry as exc:
            return "RETRY", str(exc)

    verdict = asyncio.run(go())
    assert verdict[0] == "RETRY"
    assert "placeholder/sentinel" in verdict[1]


def test_gate_ignores_empty_string_column(tmp_path):
    """The gate's strict set excludes '' — an all-empty tag column must not retry."""
    ws = tmp_path / "workspace"
    (ws / "geocache").mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    gate = make_validation_gate(sessions_dir=str(sessions), workspace=str(ws))
    # a valid layer whose extra tag column is all empty strings
    p = _write(ws / "geocache" / "buildings.gpkg", {"tag": ["", ""], "h": [10.0, 12.0]})

    result = asyncio.run(gate(_ctx({"ok": True, "output": str(p)}), "See buildings.gpkg."))
    assert result == "See buildings.gpkg."
