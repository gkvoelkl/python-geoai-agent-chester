"""V5 — level-3 redundancy / cross-checks (doc/validation-concept.md Ebene 3).

Covers `geofacts.compare_layers` + `area_length_consistency`, the `cross_check` tool
(reasonableness / aggregate / two_method), and the gate's automatic area-vs-geometry
redundancy check at level 3. Offline, no QGIS.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from _util import tools_of
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from selmakit.commands import SessionProxy

from chester.capabilities.validation import GeoValidationCapability
from chester.gate import VALID_LEVEL_KEY, make_validation_gate
from chester.geofacts import area_length_consistency, compare_layers


def _square(x0=0.0, y0=0.0, size=1000.0):
    from shapely.geometry import Polygon

    return Polygon([(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size)])


def _poly_gpkg(path: Path, area_value: float, col: str = "area_m2") -> Path:
    """A 1000×1000 m square (geometric area 1e6 m²) with a stored area column."""
    import geopandas as gpd

    gpd.GeoDataFrame(
        {"id": [1], col: [area_value]}, geometry=[_square()], crs="EPSG:25832"
    ).to_file(path, driver="GPKG")
    return path


def _table_gpkg(path: Path, ids, values, col="val") -> Path:
    import geopandas as gpd
    from shapely.geometry import Point

    gpd.GeoDataFrame(
        {"key": ids, col: values},
        geometry=[Point(11.0 + i * 1e-4, 49.0) for i in range(len(ids))],
        crs="EPSG:25832",
    ).to_file(path, driver="GPKG")
    return path


# ── geofacts.compare_layers ──────────────────────────────────────────────────


def test_compare_layers_agreement(tmp_path):
    a = _table_gpkg(tmp_path / "a.gpkg", [1, 2, 3], [10.0, 20.0, 30.0], col="h")
    b = _table_gpkg(tmp_path / "b.gpkg", [1, 2, 3], [11.0, 19.0, 31.0], col="h2")
    r = compare_layers(str(a), "h", str(b), "h2", "key")
    assert r["matched"] == 3 and r["compared"] == 3
    assert r["max_abs_diff"] == 1.0


def test_compare_layers_no_match(tmp_path):
    a = _table_gpkg(tmp_path / "a.gpkg", [1, 2], [10.0, 20.0], col="h")
    b = _table_gpkg(tmp_path / "b.gpkg", [7, 8], [10.0, 20.0], col="h")
    r = compare_layers(str(a), "h", str(b), "h", "key")
    assert r["matched"] == 0 and r["compared"] == 0


# ── geofacts.area_length_consistency ─────────────────────────────────────────


def test_area_consistency_ok(tmp_path):
    r = area_length_consistency(str(_poly_gpkg(tmp_path / "ok.gpkg", 1_000_000.0)))
    assert r is not None and r["kind"] == "area"
    assert r["median_rel_diff"] < 0.01


def test_area_consistency_flags_wrong_column(tmp_path):
    r = area_length_consistency(str(_poly_gpkg(tmp_path / "bad.gpkg", 2_000_000.0)))
    assert r["median_rel_diff"] > 0.5  # stored 2e6 vs geometric 1e6 → ~100%


def test_area_consistency_none_without_column(tmp_path):
    import geopandas as gpd

    p = tmp_path / "plain.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[_square()], crs="EPSG:25832").to_file(p, driver="GPKG")
    assert area_length_consistency(str(p)) is None


def test_area_consistency_none_on_geographic_crs(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Polygon

    p = tmp_path / "geo.gpkg"
    gpd.GeoDataFrame(
        {"id": [1], "area_m2": [1.0]},
        geometry=[Polygon([(11, 49), (11.01, 49), (11.01, 49.01), (11, 49.01)])],
        crs="EPSG:4326",
    ).to_file(p, driver="GPKG")
    assert area_length_consistency(str(p)) is None


# ── cross_check tool ─────────────────────────────────────────────────────────


def _cc(tmp_path):
    return tools_of(GeoValidationCapability(workspace=str(tmp_path)))["cross_check"]


def test_cross_check_reasonableness(tmp_path):
    cc = _cc(tmp_path)
    assert cc("reasonableness", value=103, expected=100, tolerance=0.05)["ok"] is True
    assert cc("reasonableness", value=150, expected=100, tolerance=0.05)["ok"] is False


def test_cross_check_aggregate(tmp_path):
    cc = _cc(tmp_path)
    p = _table_gpkg(tmp_path / "gem.gpkg", [1, 2, 3], [40.0, 30.0, 30.0], col="pop")
    ok = cc("aggregate", path=str(p), field="pop", expected_total=100.0, tolerance=0.05)
    assert ok["ok"] is True and ok["sum"] == 100.0
    bad = cc("aggregate", path=str(p), field="pop", expected_total=200.0, tolerance=0.05)
    assert bad["ok"] is False


def test_cross_check_two_method(tmp_path):
    cc = _cc(tmp_path)
    a = _table_gpkg(tmp_path / "a.gpkg", [1, 2, 3], [10.0, 20.0, 30.0], col="h")
    b = _table_gpkg(tmp_path / "b.gpkg", [1, 2, 3], [10.5, 19.5, 30.5], col="h2")
    agree = cc("two_method", path=str(a), field="h", path_b=str(b), field_b="h2",
               key="key", tolerance=0.15)
    assert agree["ok"] is True
    b2 = _table_gpkg(tmp_path / "b2.gpkg", [1, 2, 3], [30.0, 60.0, 90.0], col="h2")
    disagree = cc("two_method", path=str(a), field="h", path_b=str(b2), field_b="h2",
                  key="key", tolerance=0.15)
    assert disagree["ok"] is False


def test_cross_check_bad_mode_and_missing_args(tmp_path):
    cc = _cc(tmp_path)
    assert cc("nope")["ok"] is False
    assert cc("reasonableness", value=1)["ok"] is False  # missing expected


# ── gate level-3 automatic redundancy ────────────────────────────────────────


def _ctx(output_path: str):
    part = ToolReturnPart(tool_name="fetch", content={"output": output_path}, tool_call_id="c1")
    req = ModelRequest(parts=[part])
    run_id = None
    try:
        req.run_id = "R1"
        run_id = "R1"
    except Exception:  # noqa: BLE001
        pass
    return SimpleNamespace(deps="s1", messages=[req], run_id=run_id, retry=0, max_retries=1)


def _make(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "geocache").mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    gate = make_validation_gate(sessions_dir=str(sessions), workspace=str(ws))
    return gate, ws / "geocache", sessions


def _run(gate, ctx, answer):
    async def _go():
        try:
            return "PASS", await gate(ctx, answer)
        except ModelRetry as exc:
            return "RETRY", str(exc)

    return asyncio.run(_go())


def test_gate_level3_flags_area_mismatch(tmp_path):
    gate, cache, sessions = _make(tmp_path)
    SessionProxy(str(sessions), "s1").set(VALID_LEVEL_KEY, 3)
    p = _poly_gpkg(cache / "parcels.gpkg", 2_000_000.0)  # stored 2× the true area
    verdict, out = _run(gate, _ctx(str(p)), "See parcels.gpkg.")
    assert verdict == "PASS"
    assert "area" in out and "disagrees with the geometry" in out


def test_gate_level3_silent_when_area_ok(tmp_path):
    gate, cache, sessions = _make(tmp_path)
    SessionProxy(str(sessions), "s1").set(VALID_LEVEL_KEY, 3)
    p = _poly_gpkg(cache / "parcels.gpkg", 1_000_000.0)  # correct area
    verdict, out = _run(gate, _ctx(str(p)), "See parcels.gpkg.")
    assert verdict == "PASS" and out == "See parcels.gpkg."


def test_gate_level2_does_not_run_redundancy(tmp_path):
    # area mismatch present, but level 2 must not do the level-3 redundancy check
    gate, cache, sessions = _make(tmp_path)
    SessionProxy(str(sessions), "s1").set(VALID_LEVEL_KEY, 2)
    p = _poly_gpkg(cache / "parcels.gpkg", 2_000_000.0)
    verdict, out = _run(gate, _ctx(str(p)), "See parcels.gpkg.")
    assert verdict == "PASS" and out == "See parcels.gpkg."
