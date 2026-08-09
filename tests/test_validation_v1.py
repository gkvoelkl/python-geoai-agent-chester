"""V1 — richer level-1 validation checks (doc/validation-concept.md Ebene 1).

Covers ``geofacts.attribute_facts`` (null/placeholder/out-of-range per field), the
``chester.plausibility`` domain bands, the ``sanity_check_result`` attribute
expectations, and the gate's placeholder-saturation detection. Offline, no QGIS.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from chester import plausibility
from chester.capabilities.validation import GeoValidationCapability
from chester.geofacts import attribute_facts
from chester.gate import make_validation_gate

from _util import tools_of


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
