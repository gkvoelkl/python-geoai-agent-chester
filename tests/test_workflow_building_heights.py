"""Deterministic end-to-end of the building-heights workflow (no LLM).

Runs the same tool chain the skill drives — DSM−DTM → zonal max → filter — and
asserts the correct buildings come out, proving the pipeline independently of the
model's orchestration.

Until 2026-10-04 this ran over `qgis_raster_calc`/`qgis_zonal_stats` and was skipped
wherever QGIS was missing. Those doubles are gone (Phase KQ 2b); the chain now runs on
Chester's own core and therefore everywhere.
"""

import geopandas as gpd
from _util import tools_of, write_building_sample

from chester.capabilities.geocore import GeoCoreCapability
from chester.capabilities.vector import VectorCapability


def test_building_heights_pipeline(tmp_path):
    sample = write_building_sample(tmp_path)
    ws = str(tmp_path)
    core = tools_of(GeoCoreCapability(workspace=ws))
    vector = tools_of(VectorCapability(workspace=ws))

    # height raster = DSM - DTM
    assert core["raster_calc"](
        output_path="height.tif", expression="a - b",
        a=str(sample["dsm"]), b=str(sample["dtm"]),
    )["ok"]

    # max height per building footprint
    zonal = core["zonal_stats"](
        raster_path="height.tif", zones_path=str(sample["buildings"]),
        output_path="bh.geojson", stat="max", column="h_max",
    )
    assert zonal["ok"], zonal

    # keep buildings taller than 15 m
    filtered = vector["vector_filter"](
        path="bh.geojson", expression="h_max > 15", output_path="tall.geojson"
    )
    assert filtered["ok"] and filtered["after"] == 2

    # Every intermediate (height.tif → bh.geojson → tall.geojson) chains through
    # the confined geocache/ dir, so the final output lands there too.
    tall = set(gpd.read_file(tmp_path / "geocache" / "tall.geojson")["name"])
    assert tall == sample["tall_names"]
