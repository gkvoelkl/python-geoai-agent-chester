"""Deterministic end-to-end of the building-heights workflow (no LLM).

Runs the same tool chain the skill drives — DSM−DTM → zonal max → filter — and
asserts the correct buildings come out, proving the pipeline independently of the
model's orchestration.
"""

from _util import requires_qgis, tools_of, write_building_sample

from chester.capabilities.qgis import QgisToolboxCapability
from chester.capabilities.vector import VectorCapability


@requires_qgis
def test_building_heights_pipeline(tmp_path):
    sample = write_building_sample(tmp_path)
    ws = str(tmp_path)
    qgis = tools_of(QgisToolboxCapability(workspace=ws))
    vector = tools_of(VectorCapability(workspace=ws))

    # height raster = DSM - DTM
    assert qgis["qgis_raster_calc"](
        input_a=str(sample["dsm"]), input_b=str(sample["dtm"]),
        formula="A-B", output_path="height.tif",
    )["ok"]

    # max height per building footprint
    assert qgis["qgis_zonal_stats"](
        zones_path=str(sample["buildings"]), raster_path="height.tif",
        output_path="bh.geojson", statistics=["max"], prefix="h_",
    )["ok"]

    # keep buildings taller than 15 m
    filtered = vector["vector_filter"](
        path="bh.geojson", expression="h_max > 15", output_path="tall.geojson"
    )
    assert filtered["ok"] and filtered["after"] == 2

    import geopandas as gpd

    # Every intermediate (height.tif → bh.geojson → tall.geojson) chains through
    # the confined geocache/ dir, so the final output lands there too.
    tall = set(gpd.read_file(tmp_path / "geocache" / "tall.geojson")["name"])
    assert tall == sample["tall_names"]


@requires_qgis
def test_zonal_stats_reports_the_values_it_computed(tmp_path):
    """A result the caller cannot see is a result the answer cannot report.

    From `mean-elevation-per-district` (2026-08-27): eighteen district means were
    computed correctly, the return value held nothing but a path, and the answer
    that followed showed a map and described the method — without one figure.
    """
    sample = write_building_sample(tmp_path)
    ws = str(tmp_path)
    qgis = tools_of(QgisToolboxCapability(workspace=ws))

    assert qgis["qgis_raster_calc"](
        input_a=str(sample["dsm"]), input_b=str(sample["dtm"]),
        formula="A-B", output_path="height.tif",
    )["ok"]
    r = qgis["qgis_zonal_stats"](
        zones_path=str(sample["buildings"]), raster_path="height.tif",
        output_path="bh2.geojson", statistics=["max"], prefix="h_",
    )
    assert r["ok"]

    stats = r["statistics"]["h_max"]
    assert stats["zones_with_value"] == 3  # one value per building, not one global
    assert round(stats["max"]) == 25 and round(stats["min"]) == 8
    # Named extremes: "Hoch: 25.0" reads back into an answer, "max 25.0" does not.
    assert stats["highest"].startswith("Hoch:")
    assert stats["lowest"].startswith("Klein:")
    assert "report them" in r["note"]
