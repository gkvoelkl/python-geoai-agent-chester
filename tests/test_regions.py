"""Tests for the country/CRS-aware region layer (chester/regions.py, §5.10).

All offline — detection uses the embedded simplified DE/CH/AT outlines.
"""

from __future__ import annotations

import pytest

from chester import regions


@pytest.mark.parametrize("lon,lat,expect", [
    (11.58, 48.14, "DE"),   # München — in the DE/AT bbox overlap, must be DE
    (13.74, 51.05, "DE"),   # Dresden (east)
    (16.37, 48.21, "AT"),   # Wien
    (11.40, 47.27, "AT"),   # Innsbruck — near the DE border, must be AT
    (7.45, 46.95, "CH"),    # Bern
    (2.35, 48.85, None),    # Paris — outside DACH
])
def test_detect_country(lon, lat, expect):
    assert regions.detect_country(lon, lat) == expect


def test_metric_crs_by_country_and_utm_split():
    assert regions.metric_crs("DE", 10.0) == 25832   # west of 12°E
    assert regions.metric_crs("DE", 13.0) == 25833   # east of 12°E
    assert regions.metric_crs("CH") == 2056
    assert regions.metric_crs("AT") == 31287
    assert regions.metric_crs(None) == 4326


def test_region_profile_austria_bbox():
    # a Vienna bbox → AT connectors + EPSG:31287
    p = regions.region_profile([16.36, 48.20, 16.38, 48.21])
    assert p["detected_country"] == "AT"
    assert p["recommended_crs"] == 31287
    assert p["connectors"]["terrain"] == "fetch_austria_dem"
    assert p["connectors"]["boundaries"] == "fetch_austria_boundaries"


def test_region_profile_point_form_and_fallback():
    de = regions.region_profile([11.58, 48.14])  # [lon, lat] point form
    assert de["detected_country"] == "DE" and de["recommended_crs"] == 25832
    out = regions.region_profile([2.35, 48.85])  # Paris → fallback
    assert out["detected_country"] is None
    assert out["connectors"]["terrain"] == "fetch_dem"


def test_region_profile_rejects_bad_input():
    with pytest.raises(ValueError):
        regions.region_profile([1.0, 2.0, 3.0])  # neither point nor bbox


def test_region_profile_tool_wired_in_discovery():
    from agent_build import _capability_tools
    from chester.capabilities import DataDiscoveryCapability

    tools = _capability_tools(DataDiscoveryCapability(workspace="/tmp/ws"))
    assert "region_profile" in tools
