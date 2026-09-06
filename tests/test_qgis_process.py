"""Tests for the QgisProcess runner: search filtering (unit) + describe/run (qgis)."""

from pathlib import Path

import pytest
from _util import requires_qgis, write_point

from chester.qgis_env import QgisEnv, find_gisbase
from chester.qgis_process import QgisProcess, QgisProcessError


def test_search_filters_by_keyword():
    # Build a QgisProcess without touching the real binary, inject a catalog.
    qp = QgisProcess.__new__(QgisProcess)
    qp._algorithms = {
        "native:buffer": {
            "name": "Buffer", "group": "Vector geometry",
            "short_description": "Computes a buffer area", "tags": ["buffer"],
            "provider": "native",
        },
        "native:slope": {
            "name": "Slope", "group": "Raster terrain",
            "short_description": "Slope from a DEM", "tags": ["slope"],
            "provider": "gdal",
        },
    }
    ids = [h["id"] for h in qp.search("buffer")]
    assert "native:buffer" in ids
    assert "native:slope" not in ids


def test_search_matches_tags_and_description():
    qp = QgisProcess.__new__(QgisProcess)
    qp._algorithms = {
        "native:slope": {
            "name": "Slope", "group": "g", "short_description": "steepness",
            "tags": ["gradient"], "provider": "gdal",
        }
    }
    assert [h["id"] for h in qp.search("gradient")] == ["native:slope"]
    assert [h["id"] for h in qp.search("steepness")] == ["native:slope"]


def test_search_matches_all_tokens_in_any_order():
    # Multi-word keywords match on all tokens, not as one literal phrase:
    # "join attributes location" must find "Join attributes by location".
    qp = QgisProcess.__new__(QgisProcess)
    qp._algorithms = {
        "native:joinattributesbylocation": {
            "name": "Join attributes by location", "group": "Vector general",
            "short_description": "Joins by a spatial relationship",
            "tags": ["join"], "provider": "native",
        },
        "native:buffer": {
            "name": "Buffer", "group": "g", "short_description": "buffer area",
            "tags": ["buffer"], "provider": "native",
        },
    }
    ids = [h["id"] for h in qp.search("join attributes location")]
    assert ids == ["native:joinattributesbylocation"]


def test_search_falls_back_to_best_partial_match():
    # No algorithm contains every token, so the strict AND match is empty.
    # Rather than return nothing, fall back to the highest token overlap —
    # the real fix for descriptive queries like "field calculator area".
    qp = QgisProcess.__new__(QgisProcess)
    qp._algorithms = {
        "native:fieldcalculator": {
            "name": "Field calculator", "group": "Vector table",
            "short_description": "Computes a new field", "tags": ["field"],
            "provider": "native",
        },
        "native:buffer": {
            "name": "Buffer", "group": "g", "short_description": "buffer area",
            "tags": ["buffer"], "provider": "native",
        },
    }
    # "field" + "calculator" hit fieldcalculator (2 tokens); "area" hits buffer
    # (1 token). Best overlap wins, so the field calculator ranks alone.
    ids = [h["id"] for h in qp.search("field calculator area")]
    assert ids == ["native:fieldcalculator"]


def test_search_strict_when_full_match_exists():
    # When some algorithm matches ALL tokens, weaker partial matches are dropped
    # (the fallback only kicks in when nothing matches everything).
    qp = QgisProcess.__new__(QgisProcess)
    qp._algorithms = {
        "native:full": {
            "name": "Buffer area", "group": "g", "short_description": "d",
            "tags": [], "provider": "native",
        },
        "native:partial": {
            "name": "Buffer", "group": "g", "short_description": "d",
            "tags": [], "provider": "native",
        },
    }
    ids = [h["id"] for h in qp.search("buffer area")]
    assert ids == ["native:full"]


@requires_qgis
def test_describe_buffer_lists_parameters():
    d = QgisProcess().describe("native:buffer")
    assert d["id"] == "native:buffer"
    assert "DISTANCE" in d["parameters"]
    assert "INPUT" in d["parameters"]


@requires_qgis
def test_run_buffer_creates_output(tmp_path):
    pt = write_point(tmp_path / "p.geojson", 500000, 5600000, "EPSG:25832")
    out = tmp_path / "buf.geojson"
    res = QgisProcess().run(
        "native:buffer",
        {"INPUT": str(pt), "DISTANCE": 50, "OUTPUT": str(out)},
    )
    assert res["results"]["OUTPUT"]
    assert out.exists()


# ── GRASS availability ──────────────────────────────────────────────────
#
# QGIS lists all 307 `grass:*` algorithms whether or not GRASS can run them: the
# provider registers from description files and only discovers a missing GISBASE
# when an algorithm is invoked. Both tests below inject `_env` explicitly — unlike
# the search tests above, this code path reads it, and a `__new__`-built instance
# without one would fail for the wrong reason.

_GRASS_CATALOG = {
    "grass:r.watershed": {
        "name": "r.watershed", "group": "Raster (r.*)",
        "short_description": "Watershed basin analysis", "tags": ["watershed"],
        "provider": "grass",
    },
    "native:fillsinkswangliu": {
        "name": "Fill sinks", "group": "Raster terrain",
        "short_description": "Fill sinks in a watershed DEM", "tags": ["watershed"],
        "provider": "native",
    },
}


def _proc(*, grass: bool) -> QgisProcess:
    env = {"QT_QPA_PLATFORM": "offscreen"}
    if grass:
        env["GISBASE"] = "/Applications/GRASS-8.4.app/Contents/Resources"
    qp = QgisProcess.__new__(QgisProcess)
    qp._env = QgisEnv(bin=Path("/nonexistent/qgis_process"), env=env)
    qp.timeout = 60
    qp._algorithms = dict(_GRASS_CATALOG)
    return qp


def test_search_flags_grass_as_unrunnable_when_gisbase_missing():
    hit = next(h for h in _proc(grass=False).search("watershed")
               if h["id"] == "grass:r.watershed")
    assert hit["available"] is False
    assert "GRASS is not installed" in hit["unavailable_reason"]


def test_search_leaves_grass_unflagged_when_gisbase_present():
    hit = next(h for h in _proc(grass=True).search("watershed")
               if h["id"] == "grass:r.watershed")
    assert "available" not in hit
    assert "unavailable_reason" not in hit


def test_search_ranks_runnable_algorithm_before_unrunnable_one():
    # "watershed" is in both names, so only availability may separate them.
    assert _proc(grass=False).search("watershed")[0]["id"] == "native:fillsinkswangliu"


def test_run_refuses_grass_instead_of_failing_cryptically():
    # The binary path is bogus: reaching the subprocess at all would raise
    # something else, so this also proves the guard fires before invocation.
    with pytest.raises(QgisProcessError, match="GRASS is not installed"):
        _proc(grass=False).run("grass:r.watershed", {"elevation": "dem.tif"})


def test_run_admits_native_algorithms_regardless_of_grass():
    # Reaching the (bogus) binary is the assertion: the guard let this one through.
    with pytest.raises(FileNotFoundError):
        _proc(grass=False).run("native:fillsinkswangliu", {"INPUT": "dem.tif"})


def test_find_gisbase_rejects_a_bundle_without_the_version_marker(tmp_path, monkeypatch):
    empty = tmp_path / "GRASS-9.9.app"
    (empty / "Contents" / "Resources" / "bin").mkdir(parents=True)
    monkeypatch.setenv("CHESTER_GRASS_APP", str(empty))
    assert find_gisbase() != empty / "Contents" / "Resources"


def test_find_gisbase_accepts_a_bundle_with_the_version_marker(tmp_path, monkeypatch):
    good = tmp_path / "GRASS-8.4.app" / "Contents" / "Resources"
    (good / "etc").mkdir(parents=True)
    (good / "etc" / "VERSIONNUMBER").write_text("8.4.2 exported\n")
    monkeypatch.setenv("CHESTER_GRASS_APP", str(good.parent.parent))
    assert find_gisbase() == good


def test_search_maps_task_language_to_catalogue_language():
    # The catalogue says "watershed"; the task says "Abflussakkumulation". Measured
    # 2026-09-03: without the synonym this query returned [], and the agent then
    # treated flow accumulation as unavailable.
    ids = [h["id"] for h in _proc(grass=True).search("abflussakkumulation")]
    assert "grass:r.watershed" in ids


def test_search_finds_the_join_a_german_prompt_asks_for():
    # Der Prompt sagt „verbinde die Tabelle über den AGS", der Katalog sagt „join".
    # Gemessen 2026-09-05 (`join-leading-zero-ags`): Ohne dieses Wortpaar findet eine
    # deutschsprachige Suche `native:joinattributestable` nicht — den Weg, den die
    # Instruktion für Statistik-Joins ausdrücklich vorschreibt.
    qp = _proc(grass=False)
    qp._algorithms = {
        "native:joinattributestable": {
            "name": "Join attributes by field value", "group": "Vector general",
            "short_description": "Joins a table to a vector layer by field value",
            "tags": ["join"], "provider": "native",
        },
    }
    for word in ("verbinden", "verbinde", "zusammenführen"):
        ids = [h["id"] for h in qp.search(word)]
        assert "native:joinattributestable" in ids, word
