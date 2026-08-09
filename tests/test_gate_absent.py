"""Gate check: "claimed but never produced" — the answer names an output file that
does not exist on disk (e.g. the agent says it saved a .gpkg but no tool wrote it).

Advisory (a note, never a retry). Runs even when the run produced nothing at all,
so the phantom-file case is caught. Offline, no model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai.messages import ModelRequest, ToolReturnPart

from chester.gate import _absent_claims, make_validation_gate


def _good_gpkg(path: Path) -> Path:
    import geopandas as gpd
    from shapely.geometry import Point

    gpd.GeoDataFrame({"x": [1]}, geometry=[Point(11.0, 49.0)], crs="EPSG:25832").to_file(
        path, driver="GPKG"
    )
    return path


# ── the pure extractor ───────────────────────────────────────────────────────


def test_absent_claims_flags_missing_output_file(tmp_path):
    out = _absent_claims("Ich habe das Ergebnis in buildings.gpkg gespeichert.", str(tmp_path))
    assert out == ["buildings.gpkg"]


def test_absent_claims_silent_when_file_exists(tmp_path):
    (tmp_path / "geocache").mkdir()
    _good_gpkg(tmp_path / "geocache" / "buildings.gpkg")
    assert _absent_claims("Result saved to buildings.gpkg.", str(tmp_path)) == []


def test_absent_claims_ignores_urls_and_non_output_ext(tmp_path):
    text = "See https://example.org/data.tif and the source layer roads.shp."
    # .tif is behind a URL (skipped); .shp is not an output extension (not scanned)
    assert _absent_claims(text, str(tmp_path)) == []


def test_absent_claims_dedupes(tmp_path):
    out = _absent_claims("map.html here and again map.html there", str(tmp_path))
    assert out == ["map.html"]


# ── gate integration ─────────────────────────────────────────────────────────


def _ctx(tool_output):
    part = ToolReturnPart(tool_name="fetch", content=tool_output, tool_call_id="c1")
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
    return make_validation_gate(sessions_dir=str(sessions), workspace=str(ws)), ws


def test_gate_notes_phantom_file_even_with_no_output(tmp_path):
    """The buildings-to-geopackage case: the run wrote nothing, the answer claims a
    .gpkg — the gate must still note it (runs before the no-paths early return)."""
    gate, _ws = _make(tmp_path)
    out = asyncio.run(
        gate(_ctx({"ok": True}), "Ich habe die Gebäude als buildings_4326.gpkg exportiert.")
    )
    assert "Validation note" in out and "buildings_4326.gpkg" in out


def test_gate_silent_when_claimed_file_exists(tmp_path):
    gate, ws = _make(tmp_path)
    _good_gpkg(ws / "geocache" / "buildings_4326.gpkg")
    answer = "Ich habe die Gebäude als buildings_4326.gpkg exportiert."
    out = asyncio.run(gate(_ctx({"ok": True, "output": str(ws / "geocache" / "buildings_4326.gpkg")}), answer))
    assert out == answer  # produced + exists → no note
