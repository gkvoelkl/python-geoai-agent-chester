"""Deterministic tool coverage, including ``|`` alternatives (testprompt.tool_coverage)."""

from __future__ import annotations

from testprompt import tool_coverage


def test_plain_entries_count_each():
    coverage, missing = tool_coverage(["geocode", "fetch_lod2"], ["geocode"])
    assert coverage == 0.5
    assert missing == ["fetch_lod2"]


def test_alternative_satisfied_by_either_branch():
    want = ["geocode", "qgis_show_3d|render_buildings_3d"]
    web, _ = tool_coverage(want, ["geocode", "render_buildings_3d"])
    qgis, _ = tool_coverage(want, ["geocode", "qgis_show_3d"])
    assert web == qgis == 1.0


def test_alternative_counts_once_when_both_called():
    # Both branches present must not score above a single satisfied entry.
    coverage, missing = tool_coverage(
        ["geocode", "qgis_show_3d|render_buildings_3d"],
        ["geocode", "qgis_show_3d", "render_buildings_3d"],
    )
    assert coverage == 1.0
    assert missing == []


def test_alternative_missing_is_reported_verbatim():
    coverage, missing = tool_coverage(["geocode", "qgis_show_3d|render_buildings_3d"], ["geocode"])
    assert coverage == 0.5
    assert missing == ["qgis_show_3d|render_buildings_3d"]


def test_whitespace_around_alternatives_is_tolerated():
    coverage, _ = tool_coverage(["qgis_show_3d | render_buildings_3d"], ["render_buildings_3d"])
    assert coverage == 1.0


def test_no_expectation_yields_none():
    coverage, missing = tool_coverage([], ["geocode"])
    assert coverage is None
    assert missing == []
