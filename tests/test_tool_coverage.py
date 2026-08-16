"""Deterministic tool metrics: coverage with ``|`` alternatives, and effort.

``tool_coverage`` measures *reach* (how much of the plan was met),
``tool_effort`` measures *cost* (how many calls it took) — the pair, not either
alone, describes a run: three expected tools hit in thirty calls is 100% coverage.
"""

from __future__ import annotations

from testprompt import tool_coverage, tool_effort


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


def test_effort_counts_repeats_but_not_as_distinct_tools():
    # The case coverage cannot see: the plan is met, the run still wandered.
    effort = tool_effort(
        ["geocode", "osm_features"], ["geocode", "osm_features", "osm_features", "osm_features"]
    )
    assert effort["calls"] == 4
    assert effort["distinct"] == 2
    assert effort["per_step"] == 2.0
    assert effort["offplan"] == []


def test_effort_lists_offplan_tools_once_and_in_call_order():
    effort = tool_effort(["geocode"], ["vector_info", "geocode", "check_crs", "vector_info"])
    assert effort["offplan"] == ["vector_info", "check_crs"]


def test_effort_resolves_alternatives_like_coverage():
    # Either branch of an `a|b` entry counts as planned, never as off-plan.
    effort = tool_effort(["qgis_show_3d|render_buildings_3d"], ["render_buildings_3d"])
    assert effort["offplan"] == []


def test_effort_without_expectation_has_no_detour_factor():
    effort = tool_effort([], ["geocode", "geocode"])
    assert effort["per_step"] is None
    assert effort["calls"] == 2
    # Nothing was planned, so every call is off-plan — reported, not scored.
    assert effort["offplan"] == ["geocode"]
