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

from chester.gate import _absent_claims
from chester.runtime.gatehook import make_validation_gate


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


def test_a_corrected_link_clears_the_earlier_mangled_one(tmp_path):
    """A file is missing only when **no** mention points at it.

    Measured 2026-09-05 (`street-buildings-then-refine`, step 1): the model first wrote
    the link as `(_Users/…/lappersdorf_map.html)` — underscore instead of a leading slash
    — the gate reported it, and the corrected absolute form followed in the same answer.
    The old shortcut via `seen` judged the first spelling and skipped every further one:
    it reported "not present" about a file that existed and was linked correctly three
    lines further down.
    """
    (tmp_path / "geocache").mkdir()
    (tmp_path / "geocache" / "map.html").write_text("<html></html>", encoding="utf-8")
    text = ("Karte: [x](_Users/wrong/map.html)\n\n"
            f"Hier ist die Karte: [map.html]({tmp_path}/geocache/map.html)")
    assert _absent_claims(text, str(tmp_path)) == []


def test_a_file_named_only_wrongly_is_still_absent(tmp_path):
    """The relaxation must not let the phantom case through as well."""
    text = "Karte: [x](_Users/wrong/map.html) und nochmal (_Users/other/map.html)"
    assert _absent_claims(text, str(tmp_path)) == ["map.html"]


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
    produced = {"ok": True, "output": str(ws / "geocache" / "buildings_4326.gpkg")}
    out = asyncio.run(gate(_ctx(produced), answer))
    assert out == answer  # produced + exists → no note


def _retry_ctx(retry, max_retries):
    """Nur die zwei Felder, die die Retry-Arithmetik liest."""
    return SimpleNamespace(retry=retry, max_retries=max_retries)


def test_the_mild_defect_gets_its_own_retry_when_the_budget_allows():
    """An answer defect must not starve behind the serious defect.

    Measured 2026-09-05 (`supermarket-accessibility-choropleth`): the extent tier fired,
    the agent clipped and recomputed (18 supermarkets became the right 80) — and when the
    dead link's turn came, the budget was gone. A run with two defects is by construction
    exactly the run in which the mild one starves. The second retry is harmless here,
    because the fix costs **no tool call**: the same answer again, with the path filled in.
    """
    from chester.runtime.gatehook import _may_retry, _may_retry_answer_only

    # Budget 2 (SelmaKit with retries={"tools": 4, "output": 2}):
    assert _may_retry(_retry_ctx(0, 2)) and _may_retry_answer_only(_retry_ctx(0, 2))
    assert not _may_retry(_retry_ctx(1, 2)), "der schwere Tier bleibt einmalig"
    assert _may_retry_answer_only(_retry_ctx(1, 2)), "der milde bekommt den zweiten"
    assert not _may_retry_answer_only(_retry_ctx(2, 2)), "und dann ist Schluss"


def test_the_second_retry_degrades_on_an_older_selmakit():
    """With `output: 1` everything behaves as before the change.

    SelmaKit 0.1.36 has shipped `{"tools": 4, "output": 2}` since 2026-09-06, so the
    second pot is in effect. This guarantee stays anyway: an older SelmaKit — or a caller
    that lowers the budget — must break nothing, only shorten this tier's reach.
    """
    from chester.runtime.gatehook import _may_retry, _may_retry_answer_only

    for retry in (0, 1, 2):
        assert _may_retry_answer_only(_retry_ctx(retry, 1)) == _may_retry(_retry_ctx(retry, 1))
