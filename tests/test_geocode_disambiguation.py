"""Offline tests for geocode disambiguation + bbox-area sanity (no network).

These mock osmnx's Nominatim call, so they run in the default suite (unlike the
network-gated checks in test_discovery_network.py).
"""

from __future__ import annotations

import osmnx._nominatim as _nom
from _util import tools_of

from chester.capabilities.discovery import DataDiscoveryCapability, _bbox_area_km2

# Two real Nominatim-shaped hits for an ambiguous "Neustadt", plus a single hit.
_NEUSTADT_WEINSTRASSE = {
    "display_name": "Neustadt an der Weinstraße, Rhineland-Palatinate, Germany",
    "name": "Neustadt an der Weinstraße",
    "class": "boundary",
    "type": "administrative",
    "importance": 0.55,
    "lat": "49.35",
    "lon": "8.15",
    "boundingbox": ["49.2959810", "49.4009799", "8.0009672", "8.3173792"],
    "geojson": {
        "type": "Polygon",
        "coordinates": [[[8.0, 49.3], [8.3, 49.3], [8.3, 49.4], [8.0, 49.4], [8.0, 49.3]]],
    },
}
_NEUSTADT_HANNOVER = {
    "display_name": "Neustadt am Rübenberge, Region Hannover, Lower Saxony, Germany",
    "name": "Neustadt am Rübenberge",
    "class": "boundary",
    "type": "administrative",
    "importance": 0.49,
    "lat": "52.50",
    "lon": "9.45",
    "boundingbox": ["52.4387582", "52.6771116", "9.2378330", "9.6612511"],
    "geojson": None,
}


def _patch_nominatim(monkeypatch, elements):
    def fake(query, *, by_osmid=False, limit=1, polygon_geojson=True):
        return list(elements)[:limit]

    monkeypatch.setattr(_nom, "_download_nominatim_element", fake)


def test_bbox_area_km2_matches_known_extent():
    # ~1° lat × ~1° lon near 50°N ≈ 111 km × ~71.5 km ≈ 7900 km².
    area = _bbox_area_km2(7.0, 50.0, 8.0, 51.0)
    assert 7500 < area < 8200


def test_ambiguous_geocode_returns_candidates(tmp_path, monkeypatch):
    _patch_nominatim(monkeypatch, [_NEUSTADT_WEINSTRASSE, _NEUSTADT_HANNOVER])
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))

    r = tools["geocode"](query="Neustadt, Germany")

    assert r["ok"] and r["ambiguous"] is True
    assert len(r["candidates"]) == 2
    # Top hit drives the primary result.
    assert r["display_name"].startswith("Neustadt an der Weinstraße")
    assert r["bbox"] == [8.000967, 49.295981, 8.317379, 49.40098]
    assert r["area_km2"] > 0
    assert "Neustadt" in r["note"]


def test_unambiguous_geocode_has_no_candidates(tmp_path, monkeypatch):
    _patch_nominatim(monkeypatch, [_NEUSTADT_WEINSTRASSE])
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))

    r = tools["geocode"](query="Neustadt an der Weinstraße")

    assert r["ok"] and "ambiguous" not in r and "candidates" not in r
    assert r["area_km2"] > 0


def test_geocode_saves_boundary_when_polygonal(tmp_path, monkeypatch):
    _patch_nominatim(monkeypatch, [_NEUSTADT_WEINSTRASSE])
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))

    r = tools["geocode"](query="Neustadt", output_path="boundary.geojson")

    assert r["ok"] and r["boundary"]
    # Output is confined to geocache/; the returned path points there.
    from pathlib import Path

    assert Path(r["boundary"]).exists()
    assert (tmp_path / "geocache" / "boundary.geojson").exists()


def test_geocode_no_match_falls_back_to_point(tmp_path, monkeypatch):
    _patch_nominatim(monkeypatch, [])  # Nominatim returns nothing
    # …and so does Photon, which now sits between the two. Stubbed, not merely
    # expected to fail: unstubbed it reaches the live service, and it answers even
    # "nowhere-in-particular" — with a point in Ohio. This suite is offline.
    monkeypatch.setattr("chester.capabilities.discovery._photon_lookup", lambda *a, **k: [])
    monkeypatch.setattr(
        "osmnx.geocode", lambda q: (50.5, 7.1), raising=True
    )
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))

    r = tools["geocode"](query="nowhere-in-particular")

    assert r["ok"] and r["bbox"] is None
    assert r["centroid"] == [7.1, 50.5]
    assert "point match" in r["note"]
