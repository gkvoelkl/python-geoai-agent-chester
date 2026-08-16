"""V4 — level-2 visual validation wired into the gate (doc/validation-concept.md
Ebene 2, doc/visual-validation.md Phase D).

The vision roundtrip is mocked for the deterministic tests (no model, no network):
they exercise the gate's advisory wiring and `_visual_problems`' verdict parsing.
A real end-to-end vision eval (Phase D acceptance test) is included but marked
`llm` + `network`, so it only runs with `--run-llm --run-network` and a configured
`model.vision_model`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from selmakit.commands import SessionProxy

import chester.capabilities.mapoutput as mapoutput
from chester.gate import VALID_LEVEL_KEY, _visual_problems, make_validation_gate


def _good_gpkg(path: Path) -> Path:
    import geopandas as gpd
    from shapely.geometry import Point

    gpd.GeoDataFrame(
        {"x": [1]}, geometry=[Point(11.0, 49.0)], crs="EPSG:25832"
    ).to_file(path, driver="GPKG")
    return path


def _empty_gpkg(path: Path) -> Path:
    import geopandas as gpd

    gpd.GeoDataFrame({"x": []}, geometry=[], crs="EPSG:25832").to_file(path, driver="GPKG")
    return path


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


def _make(tmp_path: Path, vision_model: str = "mock/vision"):
    ws = tmp_path / "workspace"
    (ws / "geocache").mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    gate = make_validation_gate(
        sessions_dir=str(sessions), workspace=str(ws), vision_model=vision_model
    )
    return gate, ws / "geocache", sessions


def _mock_vision(monkeypatch, verdict: str):
    """Stub out render + the vision roundtrip so no model/network is needed."""
    monkeypatch.setattr(mapoutput, "_render_snapshot", lambda *a, **k: (b"PNG", []))
    monkeypatch.setattr(mapoutput, "_ask_vision_model", lambda *a, **k: verdict)


# ── _visual_problems verdict parsing ─────────────────────────────────────────


def test_visual_problems_parses_problem(monkeypatch, tmp_path):
    _mock_vision(monkeypatch, "PROBLEM: the layer sits off the coast (CRS bug)")
    p = _good_gpkg(tmp_path / "x.gpkg")
    out = _visual_problems(str(p), vision_model="mock/vision", base_url="", workspace=str(tmp_path))
    assert len(out) == 1 and "off the coast" in out[0]


def test_visual_problems_ok_is_inert(monkeypatch, tmp_path):
    _mock_vision(monkeypatch, "OK")
    p = _good_gpkg(tmp_path / "x.gpkg")
    assert _visual_problems(str(p), vision_model="mock/vision", base_url="",
                            workspace=str(tmp_path)) == []


def test_visual_problems_no_image_is_inert(monkeypatch, tmp_path):
    _mock_vision(monkeypatch, "NO_IMAGE")
    p = _good_gpkg(tmp_path / "x.gpkg")
    assert _visual_problems(str(p), vision_model="mock/vision", base_url="",
                            workspace=str(tmp_path)) == []


def test_visual_problems_without_model_is_inert(tmp_path):
    p = _good_gpkg(tmp_path / "x.gpkg")
    assert _visual_problems(str(p), vision_model="", base_url="", workspace=str(tmp_path)) == []


# ── gate integration ─────────────────────────────────────────────────────────


def _run(gate, ctx, answer):
    async def _go():
        try:
            return "PASS", await gate(ctx, answer)
        except ModelRetry as exc:
            return "RETRY", str(exc)

    return asyncio.run(_go())


def test_gate_level2_appends_advisory_note_on_problem(monkeypatch, tmp_path):
    _mock_vision(monkeypatch, "PROBLEM: extent covers the whole country, not the city")
    gate, cache, sessions = _make(tmp_path)
    SessionProxy(str(sessions), "s1").set(VALID_LEVEL_KEY, 2)
    p = _good_gpkg(cache / "districts.gpkg")
    verdict, out = _run(gate, _ctx(str(p)), "Result in districts.gpkg.")
    assert verdict == "PASS"
    assert out.startswith("Result in districts.gpkg.")
    assert "Validation note" in out and "whole country" in out


def test_gate_level2_clean_visual_leaves_answer_unchanged(monkeypatch, tmp_path):
    _mock_vision(monkeypatch, "OK")
    gate, cache, sessions = _make(tmp_path)
    SessionProxy(str(sessions), "s1").set(VALID_LEVEL_KEY, 2)
    p = _good_gpkg(cache / "districts.gpkg")
    verdict, out = _run(gate, _ctx(str(p)), "Result in districts.gpkg.")
    assert verdict == "PASS" and out == "Result in districts.gpkg."


def test_gate_level1_never_runs_visual(monkeypatch, tmp_path):
    # Even if the vision model would flag it, level 1 must not call it.
    _mock_vision(monkeypatch, "PROBLEM: should never be seen at level 1")
    gate, cache, sessions = _make(tmp_path)
    SessionProxy(str(sessions), "s1").set(VALID_LEVEL_KEY, 1)
    p = _good_gpkg(cache / "districts.gpkg")
    verdict, out = _run(gate, _ctx(str(p)), "Result in districts.gpkg.")
    assert verdict == "PASS" and out == "Result in districts.gpkg."


def test_gate_structural_defect_takes_precedence_over_visual(monkeypatch, tmp_path):
    # An empty result must retry on the structural check; the visual model must not
    # even be consulted (it would raise if called).
    def _boom(*a, **k):
        raise AssertionError("visual check must not run when structural fails")

    monkeypatch.setattr(mapoutput, "_render_snapshot", _boom)
    gate, cache, sessions = _make(tmp_path)
    SessionProxy(str(sessions), "s1").set(VALID_LEVEL_KEY, 2)
    p = _empty_gpkg(cache / "flood.gpkg")
    verdict, msg = _run(gate, _ctx(str(p)), "Result in flood.gpkg.")
    assert verdict == "RETRY" and "empty" in msg


# ── Phase D — real vision eval (opt-in) ──────────────────────────────────────


@pytest.mark.llm
@pytest.mark.network
def test_visual_check_catches_misplaced_layer(tmp_path):
    """A layer written with swapped lon/lat renders off-coast; a real vision model
    over an OSM basemap should flag it — the doc's Phase D acceptance test. Skipped
    unless a vision model is configured."""
    from agent_build import _config_base_url, _config_vision_model

    vision_model = _config_vision_model()
    if not vision_model:
        pytest.skip("no model.vision_model configured")

    import geopandas as gpd
    from shapely.geometry import Point

    # Regensburg is ~(12.1, 49.0); swapping lon/lat drops it into the Arabian Sea.
    p = tmp_path / "swapped.gpkg"
    gpd.GeoDataFrame(
        {"name": ["x"]}, geometry=[Point(49.0, 12.1)], crs="EPSG:4326"
    ).to_file(p, driver="GPKG")

    out = _visual_problems(
        str(p), vision_model=vision_model, base_url=_config_base_url(), workspace=str(tmp_path)
    )
    # The vision model should notice the point is in open water. Model-dependent, so
    # this is an acceptance signal rather than a hard CI gate.
    assert out, "vision model did not flag the misplaced layer"
