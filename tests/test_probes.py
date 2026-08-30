"""Die Auswertung der Test-Level-2-Proben — ohne Modell, ohne Netz (Test-Level 1).

Eine Prüflogik, die man nur mit einem laufenden Modell testen kann, bleibt selbst
ungeprüft. Deshalb sitzt sie in `chester/probes.py` und wird hier gegen erfundene
Ebenen und Werkzeug-Rückgaben gefahren.

Die Regel, die diese Tests festnageln: gemessen wird am **erzeugten Artefakt** und an
den **Rückgabewerten der Werkzeuge**, nie am Antworttext (`doc/test-levels.md`).
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
    assert not ok and "Abweichung" in why  # die Union-statt-Summe-Falle sähe so aus


def test_area_in_a_geographic_crs_is_never_an_area(tmp_path):
    """Grad-Quadrate als Fläche durchgehen zu lassen wäre genau der Fehler."""
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
    # 32632 wäre WGS84/UTM statt des amtlichen ETRS89 — die Falle des Transform-Falls.
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
    assert not ok and "1 Nullwerte" in why  # der stille Zeilenverlust beim AGS-Join
    ok, why = check({"kind": "no_nulls", "path": "j.gpkg", "column": "fehlt"},
                    workspace=ws, tool_results=[])
    assert not ok and "fehlt" in why


def test_a_missing_output_fails_every_check(tmp_path):
    for a in ({"kind": "output_exists", "path": "x.gpkg"},
              {"kind": "features", "path": "x.gpkg", "expect": 1}):
        ok, why = check(a, workspace=tmp_path, tool_results=[])
        assert not ok and "fehlt" in why


def test_no_output_is_the_passing_answer_for_a_refusal(tmp_path):
    """Beim NDVI ohne Infrarotband ist ein Nichts die bestandene Antwort."""
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
    # Dieselbe Zahl nur im Fließtext zählt nicht — sonst prüfte man die Prosa.
    ok, why = check({"kind": "value_seen", "expect": 1576.0, "tol": 0.005},
                    workspace=tmp_path, tool_results=["Die Fläche beträgt 1576 m²."])
    assert not ok and "keiner Werkzeug-Rückgabe" in why


def test_value_seen_takes_an_absolute_tolerance(tmp_path):
    """Ein Gini von 0,2222 braucht eine absolute, keine relative Schranke."""
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
])
def test_every_shipped_task_is_well_formed(task_id):
    """Die Aufgabendatei selbst: jede Probe nennt Falle, Fixtures und Prüfungen."""
    import json
    from pathlib import Path

    from chester.probes import KINDS

    tasks = {
        json.loads(line)["id"]: json.loads(line)
        for line in Path("agent-probe-tasks.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    t = tasks[task_id]
    assert t["prompt_de"] and t["trap"] and t["operation"]
    assert t["assertions"], "eine Probe ohne Prüfung misst nichts"
    for a in t["assertions"]:
        assert a["kind"] in KINDS

    # Die Fixtures liegen eingecheckt bei; fehlen sie trotzdem (jemand hat sie
    # gelöscht, ein sparsamer Checkout), prüft dieser Test hier nichts, statt
    # `./check.sh` rot zu färben — dieselbe Regel wie `_unpublished_or_skip`.
    fixtures = Path("samples/probe")
    if not fixtures.is_dir():
        pytest.skip("samples/probe/ fehlt — `uv run python samples/make_probe_fixtures.py`")
    for fixture in t.get("fixtures", []):
        assert (fixtures / fixture).is_file(), f"Fixture fehlt: {fixture}"


def test_a_probe_may_carry_its_own_deadline():
    """Eine Absage braucht mehr Luft als eine Rechenaufgabe.

    `ndvi-without-nir` handelte am 2026-08-30 sachlich richtig — es entstand keine
    Datei — sagte es aber nicht in 180 s. Durchgefallen war die Geduld des
    Prüfstands, nicht das Modell. Der Deckel steht deshalb bei der Aufgabe, neben
    der Falle, wo er begründet werden kann.
    """
    from chester.probes import effective_timeout

    assert effective_timeout({"timeout_s": 420}, 180) == 420.0
    assert effective_timeout({}, 180) == 180.0  # ohne eigenen Wert gilt der vorgegebene
    assert effective_timeout({"timeout_s": 0}, 180) == 180.0  # 0 ist kein Deckel
