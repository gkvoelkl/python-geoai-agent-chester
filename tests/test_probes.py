"""Evaluating the Test-Level-2 probes — without a model, without a network (Test-Level 1).

Checking logic that can only be tested with a running model stays unchecked itself. So
it lives in `chester/probes.py` and is run here against invented layers and tool returns.

The rule these tests nail down: measure the **produced artifact** and the **tools' return
values**, never the answer text (`doc/test-levels.md`).
"""

from __future__ import annotations

import math

import pytest

from chester.probes import check, evaluate


def _square(path, side=200.0, crs="EPSG:25832", n=1):
    import geopandas as gpd
    from shapely.geometry import box

    polys = [box(i * 1000, 0, i * 1000 + side, side) for i in range(n)]
    gpd.GeoDataFrame({"name": [f"p{i}" for i in range(n)], "wert": list(range(n))},
                     geometry=polys, crs=crs).to_file(path)


def test_area_within_tolerance_passes_and_outside_fails(tmp_path):
    _square(tmp_path / "a.gpkg", side=200)  # 40 000 m²
    ok, why = check({"kind": "area_m2", "path": "a.gpkg", "expect": 40_000, "tol": 0.01},
                    workspace=tmp_path, tool_results=[])
    assert ok and "40,000" in why

    ok, why = check({"kind": "area_m2", "path": "a.gpkg", "expect": 120_000, "tol": 0.01},
                    workspace=tmp_path, tool_results=[])
    assert not ok and "Abweichung" in why  # the union-instead-of-sum trap would look like this


def test_area_in_a_geographic_crs_is_never_an_area(tmp_path):
    """Letting square degrees pass as an area would be exactly the error."""
    _square(tmp_path / "deg.gpkg", side=0.002, crs="EPSG:4326")
    ok, why = check({"kind": "area_m2", "path": "deg.gpkg", "expect": 40_000, "tol": 0.5},
                    workspace=tmp_path, tool_results=[])
    assert not ok and "geographischen CRS" in why


def test_crs_checks(tmp_path):
    _square(tmp_path / "m.gpkg")
    ws = tmp_path
    assert check({"kind": "crs_metric", "path": "m.gpkg"}, workspace=ws, tool_results=[])[0]
    assert check({"kind": "crs_epsg", "path": "m.gpkg", "expect": 25832},
                 workspace=ws, tool_results=[])[0]
    # 32632 would be WGS84/UTM instead of the official ETRS89 — the trap of the transform case.
    ok, why = check({"kind": "crs_epsg", "path": "m.gpkg", "expect": 32632},
                    workspace=ws, tool_results=[])
    assert not ok and "25832" in why


def test_features_and_no_nulls(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    path = tmp_path / "j.gpkg"
    gpd.GeoDataFrame({"einwohner": [5482, None, 152610]},
                     geometry=[Point(i, 0) for i in range(3)], crs="EPSG:25832").to_file(path)
    ws = tmp_path
    assert check({"kind": "features", "path": "j.gpkg", "expect": 3},
                 workspace=ws, tool_results=[])[0]
    ok, why = check({"kind": "no_nulls", "path": "j.gpkg", "column": "einwohner"},
                    workspace=ws, tool_results=[])
    assert not ok and "1 Nullwerte" in why  # the silent row loss in the AGS join
    ok, why = check({"kind": "no_nulls", "path": "j.gpkg", "column": "fehlt"},
                    workspace=ws, tool_results=[])
    assert not ok and "fehlt" in why


def test_a_missing_output_fails_every_check(tmp_path):
    for a in ({"kind": "output_exists", "path": "x.gpkg"},
              {"kind": "features", "path": "x.gpkg", "expect": 1}):
        ok, why = check(a, workspace=tmp_path, tool_results=[])
        assert not ok and "fehlt" in why


def test_no_output_is_the_passing_answer_for_a_refusal(tmp_path):
    """For NDVI without an infrared band, nothing is the passing answer."""
    ok, why = check({"kind": "no_output", "glob": "*ndvi*"}, workspace=tmp_path, tool_results=[])
    assert ok and "keine Datei" in why

    (tmp_path / "probe_ndvi.tif").write_bytes(b"II*\0")
    ok, why = check({"kind": "no_output", "glob": "*ndvi*"}, workspace=tmp_path, tool_results=[])
    assert not ok and "probe_ndvi.tif" in why


def test_value_seen_searches_tool_returns_not_prose(tmp_path):
    returns = [
        {"ok": True, "output": "x.gpkg"},
        {"ok": True, "sum": 1576.0, "count": 3, "mean": 525.33},
    ]
    ok, _ = check({"kind": "value_seen", "expect": 1576.0, "tol": 0.005},
                  workspace=tmp_path, tool_results=returns)
    assert ok
    # The same number only in the prose does not count — otherwise the prose would be checked.
    ok, why = check({"kind": "value_seen", "expect": 1576.0, "tol": 0.005},
                    workspace=tmp_path, tool_results=["Die Fläche beträgt 1576 m²."])
    assert not ok and "keiner Werkzeug-Rückgabe" in why


def test_value_seen_takes_an_absolute_tolerance(tmp_path):
    """A Gini of 0.2222 needs an absolute, not a relative tolerance."""
    ok, _ = check({"kind": "value_seen", "expect": 0.2222, "tol_abs": 0.01},
                  workspace=tmp_path, tool_results=[{"gini": 0.2251}])
    assert ok
    ok, _ = check({"kind": "value_seen", "expect": 0.2222, "tol_abs": 0.01},
                  workspace=tmp_path, tool_results=[{"gini": 0.31}])
    assert not ok


def test_nan_is_never_a_match(tmp_path):
    ok, _ = check({"kind": "value_seen", "expect": 1.0, "tol": 0.5},
                  workspace=tmp_path, tool_results=[{"v": math.nan}])
    assert not ok


def test_unknown_assertion_kinds_fail_loudly(tmp_path):
    ok, why = check({"kind": "vibes"}, workspace=tmp_path, tool_results=[])
    assert not ok and "unbekannte Prüfart" in why


def test_evaluate_needs_every_assertion(tmp_path):
    _square(tmp_path / "a.gpkg")
    task = {"assertions": [
        {"kind": "output_exists", "path": "a.gpkg"},
        {"kind": "features", "path": "a.gpkg", "expect": 99},
    ]}
    passed, lines = evaluate(task, workspace=tmp_path, tool_results=[])
    assert not passed
    assert lines[0].startswith("  ✓") and lines[1].startswith("  ✗")


@pytest.mark.parametrize("task_id", [
    "buffer-in-degrees", "intersection-not-selection", "union-not-sum",
    "within-on-the-boundary", "utm-choice-germany", "join-leading-zero-ags",
    "ndvi-without-nir", "footprint-area-sum", "height-gini", "area-in-degrees",
    "points-from-a-table",
])
def test_every_shipped_task_is_well_formed(task_id):
    """The task file itself: every probe names its trap, fixtures and checks."""
    import json
    from pathlib import Path

    from chester.probes import KINDS

    tasks = {
        json.loads(line)["id"]: json.loads(line)
        for line in Path("probes/agent-probe-tasks.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    t = tasks[task_id]
    assert t["prompt_de"] and t["trap"] and t["operation"]
    assert t["assertions"], "eine Probe ohne Prüfung misst nichts"
    for a in t["assertions"]:
        assert a["kind"] in KINDS

    # The fixtures are checked in; if they are missing anyway (someone deleted them, a
    # sparse checkout), this test checks nothing instead of turning `./check.sh` red —
    # the same rule as `_unpublished_or_skip`.
    fixtures = Path("probes/fixtures")
    if not fixtures.is_dir():
        pytest.skip("probes/fixtures/ fehlt — `uv run python probes/make_fixtures.py`")
    for fixture in t.get("fixtures", []):
        assert (fixtures / fixture).is_file(), f"Fixture fehlt: {fixture}"


def test_a_probe_may_carry_its_own_deadline():
    """A refusal needs more room than a computation.

    `ndvi-without-nir` acted correctly on 2026-08-30 — no file was produced — but did not
    say so within 180 s. What failed was the test stand's patience, not the model. So the
    limit stands with the task, next to the trap, where it can be justified.
    """
    from chester.probes import effective_timeout

    assert effective_timeout({"timeout_s": 420}, 180) == 420.0
    assert effective_timeout({}, 180) == 180.0  # without a value of its own the default applies
    assert effective_timeout({"timeout_s": 0}, 180) == 180.0  # 0 is no limit


def test_a_deadline_only_decides_where_the_probe_says_so():
    """The limit bounds the time, not the verdict — except for a refusal.

    `union-not-sum` delivered exactly 100,000 m² on 2026-08-31 and was still graded FAIL,
    because the model was still phrasing its answer when the limit fell. What got
    measured was the test stand's patience. For `ndvi-without-nir`, by contrast, saying
    it is the answer — that is where `requires_finish` stands.
    """
    from chester.probes import timeout_decides

    assert timeout_decides({"requires_finish": True}) is True
    assert timeout_decides({}) is False
    assert timeout_decides({"requires_finish": False}) is False


def test_the_refusal_probe_demands_a_finish():
    """The rule stands in the task, not in the runner — here is the evidence."""
    import json
    from pathlib import Path

    from chester.probes import timeout_decides

    tasks = {json.loads(li)["id"]: json.loads(li)
             for li in Path("probes", "agent-probe-tasks.jsonl").read_text("utf-8").splitlines()
             if li.strip()}
    assert timeout_decides(tasks["ndvi-without-nir"]) is True
    assert timeout_decides(tasks["union-not-sum"]) is False
