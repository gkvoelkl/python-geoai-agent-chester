"""Tests for the enforcing validation gate (``chester/gate.py``, doc §4.1/V3).

The gate is a result-based ``output_validator``: it structurally checks datasets
the run produced *and* the answer mentions, and raises ``ModelRetry`` once on a
real defect. These tests drive it with a synthetic ``RunContext`` (a message list
carrying a ``ToolReturnPart``, the same shape ``tool_returns`` walks) — no network,
no QGIS, no live agent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from chester.gate import (
    DEFAULT_LEVEL,
    VALID_LEVEL_KEY,
    clamp_level,
    make_validation_gate,
)


def _empty_gpkg(path: Path) -> Path:
    import geopandas as gpd

    gpd.GeoDataFrame({"x": []}, geometry=[], crs="EPSG:25832").to_file(path, driver="GPKG")
    return path


def _good_gpkg(path: Path) -> Path:
    import geopandas as gpd
    from shapely.geometry import Point

    gpd.GeoDataFrame(
        {"x": [1]}, geometry=[Point(11.0, 49.0)], crs="EPSG:25832"
    ).to_file(path, driver="GPKG")
    return path


def _ctx(tool_output, *, deps: str = "s1", retry: int = 0, max_retries: int = 1):
    """A minimal RunContext stand-in carrying one tool result for this run."""
    part = ToolReturnPart(tool_name="fetch", content=tool_output, tool_call_id="c1")
    req = ModelRequest(parts=[part])
    run_id = None
    try:
        req.run_id = "R1"
        run_id = "R1"
    except Exception:  # noqa: BLE001 - older message models degrade to "all messages"
        pass
    return SimpleNamespace(
        deps=deps, messages=[req], run_id=run_id, retry=retry, max_retries=max_retries
    )


def _make(tmp_path: Path):
    """A gate bound to a temp workspace/geocache + a temp sessions dir."""
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


# ── level clamping ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [("2", 2), (3, 3), (7, 3), (-1, 0), ("x", DEFAULT_LEVEL), (None, DEFAULT_LEVEL)],
)
def test_clamp_level(raw, expected):
    assert clamp_level(raw) == expected


# ── the four core behaviours ─────────────────────────────────────────────────


def test_empty_and_mentioned_retries(tmp_path):
    gate, cache, _ = _make(tmp_path)
    p = _empty_gpkg(cache / "flood_area.gpkg")
    verdict, msg = _run(
        gate, _ctx({"ok": True, "output": str(p)}), "Saved the result to flood_area.gpkg."
    )
    assert verdict == "RETRY"
    assert "flood_area.gpkg" in msg and "empty" in msg


def test_empty_but_not_mentioned_passes(tmp_path):
    """A Q&A/number answer that doesn't name the file is left untouched."""
    gate, cache, _ = _make(tmp_path)
    p = _empty_gpkg(cache / "flood_area.gpkg")
    verdict, out = _run(
        gate, _ctx({"ok": True, "output": str(p)}), "There are no flooded areas."
    )
    assert verdict == "PASS"
    assert out == "There are no flooded areas."


def test_good_result_passes_unchanged(tmp_path):
    gate, cache, _ = _make(tmp_path)
    p = _good_gpkg(cache / "buildings_result.gpkg")
    verdict, out = _run(
        gate, _ctx({"ok": True, "output": str(p)}), "Done — see buildings_result.gpkg."
    )
    assert verdict == "PASS"
    assert out == "Done — see buildings_result.gpkg."


def test_retry_budget_spent_warns_instead_of_looping(tmp_path):
    gate, cache, _ = _make(tmp_path)
    p = _empty_gpkg(cache / "flood_area.gpkg")
    verdict, out = _run(
        gate,
        _ctx({"ok": True, "output": str(p)}, retry=1),
        "Saved to flood_area.gpkg.",
    )
    assert verdict == "PASS"
    assert out.startswith("Saved to flood_area.gpkg.")
    assert "Validation note" in out and "empty" in out


# ── level control ────────────────────────────────────────────────────────────


def test_level_zero_disables_the_gate(tmp_path):
    from selmakit.commands import SessionProxy

    gate, cache, sessions = _make(tmp_path)
    p = _empty_gpkg(cache / "flood_area.gpkg")
    # Set the level the same way /valid_level does, then a defect must pass through.
    SessionProxy(str(sessions), "s1").set(VALID_LEVEL_KEY, 0)
    verdict, out = _run(
        gate, _ctx({"ok": True, "output": str(p)}), "Saved to flood_area.gpkg."
    )
    assert verdict == "PASS"
    assert out == "Saved to flood_area.gpkg."


def test_deferred_output_is_not_gated(tmp_path):
    """A non-str output (e.g. a DeferredToolRequests) is returned untouched."""
    gate, cache, _ = _make(tmp_path)
    _empty_gpkg(cache / "flood_area.gpkg")
    sentinel = object()
    result = asyncio.run(gate(_ctx({"ok": True}), sentinel))
    assert result is sentinel
