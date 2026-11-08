"""Failure classes of bank runs (MB.2) — read off history line and protocol, no model.

Test-Level 1. The classes follow GISAgentBench's taxonomy; each case is one Chester has
shown: a run that aborted before the GIS steps, a radius for a travel time, Copernicus
for DGM1, a tool called twelve times in a row, a tool that never succeeded after its error.
"""

from __future__ import annotations

from chester import failclass as fc


def test_a_run_that_did_the_planned_steps_has_no_class():
    assert fc.classify(["osm_features", "vector_reproject", "vector_buffer"],
                       ["osm_features", "vector_reproject", "vector_buffer"]) == []


def test_missing_operations_and_their_wrong_substitute():
    assert fc.classify(["geocode", "osm_features", "vector_buffer"],
                       ["osm_features", "service_area"]) == [fc.MISSING, fc.SUBSTITUTE]
    assert fc.classify(["geocode", "fetch_dem", "sample_raster"],
                       ["geocode", "fetch_dgm1", "sample_raster"]) == [fc.MISSING, fc.SUBSTITUTE]


def test_alternatives_in_the_expectation_count_either_way():
    assert fc.classify(["fetch_dgm1", "slope"], ["fetch_dgm1|fetch_dem", "slope|qgis_run"]) == []


def test_order_and_loops():
    assert fc.ORDER in fc.classify(["vector_buffer", "vector_reproject", "osm_features"],
                                   ["osm_features", "vector_reproject", "vector_buffer"])
    looping = ["geodata_search"] * 2 + ["wfs_capabilities"] * 4 + ["wfs_features"]
    assert fc.LOOP in fc.classify(looping, ["geodata_search", "wfs_features"])


def test_a_parameter_error_counts_only_when_never_corrected(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("\n".join([
        '12:00:01 │ ← vector_filter: {"ok": false, "error": "bad expression"}',
        '12:00:05 │ ← vector_filter: {"ok": true, "output": "x.gpkg"}',
        '12:00:09 │ ← render_map: {"ok": false, "error": "column not found"}',
    ]), encoding="utf-8")
    assert fc.failed_results(log) == ["render_map"]  # the filter was fixed, the map was not
    assert fc.PARAMETER in fc.classify(["vector_filter", "render_map"], [], ["render_map"])


def test_history_lines_and_the_report():
    passed = {"passed": "True", "tools_called": "['a']", "tools_expected": "['a']"}
    failed = {"passed": "False", "test_id": "t", "tools_called": "['geocode']",
              "tools_expected": "['osm_features']"}
    logic = {"passed": "False", "test_id": "u", "tools_called": "['a']",
             "tools_expected": "['a']"}
    assert fc.classify_record(passed) == []
    assert fc.classify_record(failed) == [fc.MISSING]
    text = fc.report([passed, failed, logic])
    assert "2 FAIL" in text and "1× Operation fehlt" in text and "1× ohne Klasse" in text
    assert fc.report([passed]) == ""
