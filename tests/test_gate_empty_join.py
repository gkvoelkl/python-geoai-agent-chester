"""The gate notices a join that matched nothing — whichever way it was written.

Measured 2026-09-01 (probe `join-leading-zero-ags`): the AGS was an integer on one
side and text with a leading zero on the other. The join matched nothing, and the
layer looked complete — right feature count, right geometry, the new column present —
with not one value in it. `vector_join` has warned about exactly this since September;
a join written by hand (a snippet, `qgis_run`) has no such voice. The gate's sentinel
check did not see it either: it fired on columns full of "NULL" or -9999, and a column
of nothing at all has no populated value to call a sentinel.

A NUMERIC column with no value is flagged; a text column is not, because OSM layers
legitimately keep tag columns that a clip has emptied.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from chester import gate


def _layer(path, **cols):
    gpd.GeoDataFrame(cols, geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)],
                     crs="EPSG:25832").to_file(path)
    return str(path)


def test_a_join_on_a_lost_leading_zero_is_flagged(tmp_path):
    gemeinden = gpd.GeoDataFrame({"ags": ["09362000", "09363000"]},
                                 geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)], crs="EPSG:25832")
    einwohner = pd.DataFrame({"ags": [9362000, 9363000], "einwohner": [153094, 30001]})
    # What a snippet writes: the keys compared as given, integer against text.
    joined = gemeinden.merge(einwohner.astype({"ags": str}), on="ags", how="left")
    out = tmp_path / "gemeinden_join.gpkg"
    joined.to_file(out)
    problems = gate._structural_problems(str(out))
    assert any("einwohner" in p and "failed join" in p for p in problems), problems


def test_a_partial_join_is_not_a_failed_one(tmp_path):
    path = _layer(tmp_path / "partial.gpkg", ags=["1", "2"], einwohner=[150000.0, None])
    assert gate._structural_problems(path) == []


def test_an_emptied_text_column_stays_quiet(tmp_path):
    """OSM after a clip: `addr:street` exists and holds nothing — that is not a defect."""
    path = _layer(tmp_path / "osm.gpkg", name=["a", "b"], **{"addr:street": [None, None]})
    assert gate._structural_problems(path) == []
