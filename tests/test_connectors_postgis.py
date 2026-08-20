"""A bbox against PostGIS must be transformed, not compared across SRIDs.

`geodataset_fetch(bbox=…)` takes WGS84, like every other bbox in Chester, and used
to hand it to `ST_MakeEnvelope(…, 4326)` for comparison against the table's own
geometry. Against anything but a 4326 table that asks whether a rectangle around
x=12, y=49 **metres** touches data at x=727000, y=5434000 — and PostGIS does not
raise, it answers no. Every such fetch returned `selection matched 0 features`: a
wrong answer wearing the costume of an empty one, which is the harder of the two
to notice, because "nothing here" is a plausible result for a spatial query.

Found 2026-08-19 the first time the connector ever ran against real data (the
ATKIS Regensburg fixture, EPSG:25832): 0 rows where the same window holds 48.

These need that local database — the `postgis_test_db/` setup, which is not part of
the published repository — and skip when it is not reachable, the same way the QGIS
tests skip without QGIS. Nothing here reads a file from there: the only contact is
the connection below, so a fresh clone stays green.
"""

from __future__ import annotations

import pytest

from chester.capabilities.connectors import pg_datasets, pg_fetch

DSN = "postgresql://chester:chester@127.0.0.1:55432/atkis"
SCHEMA = "atkis"
TABLE = "f_41001_wohnbauflaeche"
# A window over the Regensburg Altstadt, in WGS84 as the tool's contract says.
ALTSTADT = [12.08, 49.010, 12.115, 49.028]


def _fixture_or_skip() -> None:
    try:
        datasets = pg_datasets(DSN, SCHEMA)
    except Exception as exc:  # noqa: BLE001 - any connection failure means "not here"
        pytest.skip(f"ATKIS-PostGIS-Fixture nicht erreichbar ({type(exc).__name__})")
    if not any(d["dataset"] == TABLE for d in datasets):
        pytest.skip(f"Fixture ohne Tabelle {TABLE}")


def test_a_wgs84_bbox_finds_features_in_a_utm_table(tmp_path):
    """The regression itself: this returned 0 before the ST_Transform."""
    _fixture_or_skip()
    result = pg_fetch(DSN, SCHEMA, TABLE, str(tmp_path / "out.gpkg"), bbox=ALTSTADT)
    assert result["ok"], result
    assert result["features"] > 0
    # The output keeps the table's CRS — the bbox is transformed, not the data.
    assert result["crs"] == "EPSG:25832"


def test_the_bbox_actually_narrows_the_selection(tmp_path):
    """A transform that quietly matched *everything* would pass the test above."""
    _fixture_or_skip()
    windowed = pg_fetch(DSN, SCHEMA, TABLE, str(tmp_path / "w.gpkg"), bbox=ALTSTADT)
    everything = pg_fetch(DSN, SCHEMA, TABLE, str(tmp_path / "a.gpkg"))
    assert windowed["features"] < everything["features"]


def test_a_bbox_far_away_still_matches_nothing(tmp_path):
    """"Empty" must stay available as an honest answer, not become unreachable."""
    _fixture_or_skip()
    result = pg_fetch(DSN, SCHEMA, TABLE, str(tmp_path / "n.gpkg"),
                      bbox=[7.0, 50.7, 7.05, 50.75])  # Bonn, far outside Regensburg
    assert result["ok"] is False and "0 features" in result["error"]


def test_an_unknown_table_is_refused(tmp_path):
    """The whitelist is the injection guard; it must not be softened by any of this."""
    _fixture_or_skip()
    result = pg_fetch(DSN, SCHEMA, "pg_class", str(tmp_path / "x.gpkg"))
    assert result["ok"] is False and "unknown table" in result["error"]
