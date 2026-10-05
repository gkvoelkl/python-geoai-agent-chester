"""`geocode` reports the area of the place, not of the rectangle around it.

Found 2026-09-19 in two real runs: Regensburg came back as ~145 km² (official ~81),
Passau as 127 (~70) — the geodesic area of the bounding box, about 1.8x too large for
a city's shape. The agent passed the figure on as the city's area and used it to judge
whether it had matched the right place. Seen again on 2026-10-03 (F+, `steep-slopes`:
"Gute Übereinstimmung (145 km², kreisfreie Stadt Regensburg)").

Nominatim returns the boundary polygon with every candidate; its area is what the name
means. The rectangle stays available as `bbox_area_km2`, and `area_basis` says which
one `area_km2` is, so a hit without a polygon does not pass for a measured area.
"""

from __future__ import annotations

from _util import tools_of
from pyproj import Geod
from shapely.geometry import shape
from test_geocode_disambiguation import _NEUSTADT_HANNOVER, _patch_nominatim

from chester.capabilities.discovery import DataDiscoveryCapability

# A triangle inside its bbox: the polygon holds half of the rectangle, the shape of
# the defect (a city never fills its bounding box).
_TRIANGLE_TOWN = {
    "display_name": "Dreieckstadt, Bayern, Deutschland",
    "class": "boundary",
    "type": "administrative",
    "importance": 0.5,
    "lat": "49.0",
    "lon": "12.1",
    "boundingbox": ["49.0", "49.1", "12.0", "12.2"],
    "geojson": {"type": "Polygon",
                "coordinates": [[[12.0, 49.0], [12.2, 49.0], [12.0, 49.1], [12.0, 49.0]]]},
}


def _geocode(tmp_path, monkeypatch, elements, query="x"):
    _patch_nominatim(monkeypatch, elements)
    return tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))["geocode"](query=query)


def test_the_area_is_the_boundary_polygons(tmp_path, monkeypatch):
    r = _geocode(tmp_path, monkeypatch, [_TRIANGLE_TOWN])
    polygon = abs(Geod(ellps="WGS84").geometry_area_perimeter(
        shape(_TRIANGLE_TOWN["geojson"]))[0]) / 1e6
    assert r["area_basis"] == "boundary"
    assert abs(r["area_km2"] - polygon) < 0.1
    # The rectangle is still there, honestly named, and about twice the triangle.
    assert 1.9 < r["bbox_area_km2"] / r["area_km2"] < 2.1


def test_without_a_polygon_the_bbox_says_it_is_a_bbox(tmp_path, monkeypatch):
    r = _geocode(tmp_path, monkeypatch, [_NEUSTADT_HANNOVER])
    assert r["area_basis"] == "bbox"
    assert r["area_km2"] == r["bbox_area_km2"]


def test_every_candidate_carries_its_own_basis(tmp_path, monkeypatch):
    r = _geocode(tmp_path, monkeypatch, [_TRIANGLE_TOWN, _NEUSTADT_HANNOVER])
    assert [c["area_basis"] for c in r["candidates"]] == ["boundary", "bbox"]
    assert "km²" in r["note"]  # the note quotes the primary area, now the polygon's
