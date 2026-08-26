"""Pure unit tests for the OSM boundary clip (no network).

The defect these guard against is not a crash but a *plausible wrong number*:
`osm_features(place=…)` used to return every feature that touched the area, uncut,
while its own docstring promised a clip. In `road-impact-greenspace-100m`
(2026-08-26) that turned 8.93 km² of green space inside Regensburg into 35.06 km²
— one forest, 97 % of it outside the city, came along whole — and the reported
share fell from 96 % to 26 % without anything looking broken.
"""

from __future__ import annotations

from chester.osmclip import clip_to_boundary, clip_warning


def _layer(boxes):
    """A metric layer (EPSG:25832) so areas are exact metres, not degrees."""
    import geopandas as gpd
    from shapely.geometry import box

    return gpd.GeoDataFrame(
        {"name": [f"f{i}" for i in range(len(boxes))]},
        geometry=[box(*b) for b in boxes],
        crs="EPSG:25832",
    )


def _boundary():
    from shapely.geometry import box

    return box(0, 0, 2000, 2000)  # 4 km²


def test_a_feature_reaching_past_the_boundary_is_cut_to_it():
    inside = (100, 100, 600, 600)  # 0.25 km², wholly inside
    crossing = (1500, 1500, 3500, 3500)  # 4 km², of which 0.25 km² inside
    clipped, report = clip_to_boundary(_layer([inside, crossing]), _boundary())

    assert len(clipped) == 2  # both survive, one of them smaller
    assert round(clipped.geometry.area.sum(), 1) == 500_000.0  # 0.25 + 0.25 km²
    assert report["clipped_to_place"] is True
    assert report["features_trimmed"] == 1
    assert report["features_dropped"] == 0
    assert report["area_outside_km2"] == 3.75  # what the old behaviour counted in


def test_a_feature_wholly_outside_is_dropped():
    clipped, report = clip_to_boundary(
        _layer([(100, 100, 600, 600), (5000, 5000, 6000, 6000)]), _boundary()
    )
    assert list(clipped["name"]) == ["f0"]
    assert report["features_dropped"] == 1
    assert report["features_trimmed"] == 0


def test_nothing_to_cut_reports_nothing():
    """A warning that fires on every call is one the reader learns to skip."""
    clipped, report = clip_to_boundary(_layer([(100, 100, 600, 600)]), _boundary())
    assert len(clipped) == 1
    assert report["features_trimmed"] == 0
    assert report["features_dropped"] == 0
    assert report["area_outside_km2"] == 0.0
    assert clip_warning(report, "Regensburg") is None


def test_the_warning_names_the_cost_and_the_way_out():
    _, report = clip_to_boundary(
        _layer([(1500, 1500, 3500, 3500), (5000, 5000, 6000, 6000)]), _boundary()
    )
    note = clip_warning(report, "Regensburg")
    assert note is not None
    assert "Regensburg" in note
    assert "1 feature(s) crossed it and were cut" in note
    assert "1 feature(s) fell outside entirely" in note
    assert "4.75 km2" in note  # 3.75 from the cut feature + 1.0 from the dropped one
    assert "clip=false" in note  # the escape hatch is named, not hidden


def test_a_cut_that_leaves_two_pieces_is_not_counted_as_a_loss(monkeypatch):
    """Row counts lie here: a clip can hand back MORE rows than it got.

    Measured against Regensburg on 2026-08-26: 314 features in, 324 rows out —
    `keep_geom_type` returns the surviving parts of a cut multipart feature as
    separate rows. The first version subtracted lengths and reported
    `features_dropped: -10`, a number that cannot exist. The index knows which
    inputs survived; lengths do not.
    """
    import geopandas as gpd

    layer = _layer([(100, 100, 600, 600), (1500, 1500, 3500, 3500)])
    real_clip = gpd.clip

    def splitting_clip(gdf, boundary, keep_geom_type=False):
        out = real_clip(gdf, boundary, keep_geom_type=keep_geom_type)
        return out.iloc[[0, 1, 1]]  # the second feature came back in two pieces

    monkeypatch.setattr(gpd, "clip", splitting_clip)
    clipped, report = clip_to_boundary(layer, _boundary())

    assert len(clipped) == 3  # three rows …
    assert report["features_dropped"] == 0  # … from two inputs, none lost
    assert report["features_split"] == 1
    assert report["features_trimmed"] == 1


def test_line_geometry_survives_the_clip_without_becoming_a_collection():
    """keep_geom_type: a cut layer must not change type under the caller."""
    import geopandas as gpd
    from shapely.geometry import LineString

    lines = gpd.GeoDataFrame(
        {"name": ["road"]},
        geometry=[LineString([(1000, 1000), (5000, 1000)])],
        crs="EPSG:25832",
    )
    clipped, report = clip_to_boundary(lines, _boundary())
    assert list(clipped.geometry.geom_type) == ["LineString"]
    assert round(clipped.geometry.length.sum()) == 1000  # 1000→2000 survives
    assert report["area_outside_km2"] == 0.0  # a line has no area to lose
