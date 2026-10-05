"""QGIS capability tests: geometric correctness + workspace path resolution."""

from pathlib import Path

import numpy as np
from _util import (
    requires_qgis,
    tools_of,
    write_point,
    write_slope_dtm,
)

from chester.capabilities.qgis import QgisToolboxCapability

# This file checks the **QGIS capability**. If QGIS is switched off
# (`geodata.use_qgis: false`) or not installed, `agent_build.geo_capabilities()` does not
# wire it at all — then there is nothing to check here. Until 2026-09-06 fourteen of these
# tests failed in QGIS-less mode instead of skipping: they check pure argument validation,
# but the tools fetch their parameter schema via `qgis_describe` before looking at their
# own arguments. The geopandas side covers the same guarantees in
# `test_geoops.py`/`test_rasterops.py`/`test_networkops.py`.
pytestmark = requires_qgis


@requires_qgis
def test_slope_is_45_degrees_on_unit_slope(tmp_path):
    dtm = write_slope_dtm(tmp_path / "dtm.tif")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"]("native:slope", {"INPUT": str(dtm), "OUTPUT": "slope.tif"})
    assert r["ok"]

    import rasterio

    slope = rasterio.open(tmp_path / "geocache" / "slope.tif").read(1)[2:-2, 2:-2]  # drop edges
    assert abs(float(slope.mean()) - 45.0) < 1.0


@requires_qgis
def test_bad_parameters_return_error_not_crash(tmp_path):
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"]("native:buffer", {"INPUT": "/no/such/file.geojson"})
    assert r["ok"] is False
    assert "error" in r


@requires_qgis
def test_join_param_path_is_resolved(tmp_path):
    # native:joinattributesbylocation takes a second layer under JOIN — a path
    # key that must go through resolve_path (the earlier bug left it unresolved,
    # so qgis_process could not find "./workspace/x.geojson").
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    write_point(cache / "a.geojson", 500000, 5600000, "EPSG:25832")
    write_point(cache / "b.geojson", 500000, 5600000, "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"](
        "native:joinattributesbylocation",
        {
            "INPUT": "./workspace/a.geojson",
            "JOIN": "./workspace/b.geojson",
            "PREDICATE": [0],
            "OUTPUT": "joined.geojson",
        },
    )
    assert r["ok"]
    assert "geocache" in r["inputs"]["JOIN"]  # the JOIN path was resolved


@requires_qgis
def test_list_valued_layers_param_is_resolved(tmp_path):
    # native:mergevectorlayers takes LAYERS as a *list* of paths; every element
    # must go through resolve_path (the earlier bug left them unresolved, so
    # qgis_process could not find "./workspace/x.gpkg").
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    write_point(cache / "a.gpkg", 500000, 5600000, "EPSG:25832")
    write_point(cache / "b.gpkg", 500001, 5600001, "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"](
        "native:mergevectorlayers",
        {
            "LAYERS": ["./workspace/a.gpkg", "./workspace/b.gpkg"],
            "OUTPUT": "merged.gpkg",
        },
    )
    assert r["ok"]
    # Each LAYERS element resolved to an absolute geocache path.
    assert all("geocache" in p for p in r["inputs"]["LAYERS"])

    import geopandas as gpd

    assert len(gpd.read_file(cache / "merged.gpkg")) == 2


def test_qgis_run_survives_a_table_without_geometry(tmp_path):
    """A CSV has no geometry — the diagnosis must not break on that.

    Found 2026-09-01 while replaying the dialogue case: `native:createpointslayerfromtable`
    is the canonical route from geocoded addresses to a point layer, and it raised
    `AttributeError: 'DataFrame' object has no attribute 'geom_type'` — the access was the
    only line outside the try whose comment reads "a diagnostic must never break the
    run". To the agent the right route thus looked impossible; it fell back on
    hand-written PyQGIS (37 calls, no map).
    """
    from chester.capabilities.qgis import _geometry_types

    csv = tmp_path / "adressen.csv"
    csv.write_text("name,lon,lat\nDomplatz 7,12.096918,49.019369\n", encoding="utf-8")
    assert _geometry_types(str(csv)) is None  # no crash, no warning


@requires_qgis
def test_points_from_a_table_is_one_call(tmp_path):
    """The whole route: table in, point layer out — without a line of PyQGIS."""
    import geopandas as gpd

    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "adressen.csv").write_text(
        "name,lon,lat\n"
        "Domplatz 7,12.096918,49.019369\n"
        "Rathausplatz 4,12.094158,49.020215\n",
        encoding="utf-8",
    )
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"]("native:createpointslayerfromtable", {
        "INPUT": "geocache/adressen.csv", "XFIELD": "lon", "YFIELD": "lat",
        "TARGET_CRS": "EPSG:4326", "OUTPUT": "punkte.gpkg"})
    assert r["ok"], r.get("error")
    gdf = gpd.read_file(cache / "punkte.gpkg")
    assert len(gdf) == 2 and set(gdf.geom_type) == {"Point"}


# ── Path parameters from the algorithm schema ───────────────────────────
#
# `_PATH_KEYS` is a hand-maintained list of names, and its own comment records two lost
# runs (POLYGONS 2026-08-19, RASTERCOPY 2026-08-23). GRASS makes the approach untenable
# rather than merely risky: all 307 algorithms name their parameters in lower case. Since
# 2026-09-03 `_resolve_params` therefore derives the path parameters from
# `qgis_describe`; the name list stays only as a fallback.


@requires_qgis
def test_lowercase_grass_output_lands_in_the_workspace(tmp_path):
    """The case that cost a dialogue turn (2026-09-03).

    `grass:r.watershed` calls its output `accumulation` — lower case and in no name list.
    Unresolved, qgis_process wrote the file into the process's working directory (the
    repo root), reported `ok: true` and returned the bare name. The agent then searched
    for a file that was never where it looked.
    """
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    write_slope_dtm(cache / "dem.tif")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"](
        "grass:r.watershed",
        {"elevation": "dem.tif", "threshold": 100, "accumulation": "acc.tif"},
    )
    assert r["ok"], r.get("error")
    produced = Path(r["results"]["accumulation"])
    assert produced.is_absolute(), "der Rückgabewert muss ein Pfad sein, kein Name"
    assert produced.exists(), "die Datei liegt nicht, wo der Rückgabewert sagt"
    assert "geocache" in str(produced)


@requires_qgis
def test_multi_output_native_algorithm_resolves_every_destination(tmp_path):
    """`native:fillsinkswangliu` calls its result `OUTPUT_FILLED_DEM`.

    That was missing from the name list too — which only matched `OUTPUT` exactly. The
    case shows that this is not only about GRASS.
    """
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    write_slope_dtm(cache / "dem.tif")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"](
        "native:fillsinkswangliu",
        {"INPUT": "dem.tif", "OUTPUT_FILLED_DEM": "filled.tif"},
    )
    assert r["ok"], r.get("error")
    produced = Path(r["results"]["OUTPUT_FILLED_DEM"])
    assert produced.is_absolute() and produced.exists()


@requires_qgis
def test_a_writing_tool_still_stamps_provenance_on_a_schema_derived_output(tmp_path):
    """The sidecar rule holds for the new path just the same — otherwise resolution would
    be fixed and the provenance lost."""
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    write_slope_dtm(cache / "dem.tif")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"](
        "grass:r.watershed",
        {"elevation": "dem.tif", "threshold": 100, "accumulation": "acc.tif"},
    )
    produced = Path(r["results"]["accumulation"])
    assert produced.with_name(produced.name + ".meta.json").exists()


# ── silent successes ────────────────────────────────────────────────────
#
# QGIS ignores unknown parameters without a word. Measured 2026-09-04 in a running
# dialogue: `native:fillsinkswangliu` with `OUTPUT` (the algorithm calls its output
# `OUTPUT_FILLED_DEM`) returned `ok: true` with `results: {}` and wrote nothing. The next
# step failed on the file that never came to be; recovery took five minutes and ended on
# a leftover from an earlier run — computed without complaint. A silent success is the
# worst form of error.


@requires_qgis
def test_an_unknown_parameter_name_is_refused_with_the_valid_ones(tmp_path):
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    write_slope_dtm(cache / "dem.tif")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"](
        "native:fillsinkswangliu", {"INPUT": "dem.tif", "OUTPUT": "filled.tif"}
    )
    assert r["ok"] is False
    # The message must name the way out, not only the error.
    assert "OUTPUT_FILLED_DEM" in r["error"]
    assert "no parameter" in r["error"]


@requires_qgis
def test_the_correctly_named_parameter_still_works(tmp_path):
    """Counter-check: the guard must not catch the correct call."""
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    write_slope_dtm(cache / "dem.tif")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"](
        "native:fillsinkswangliu", {"INPUT": "dem.tif", "OUTPUT_FILLED_DEM": "f.tif"}
    )
    assert r["ok"], r.get("error")
    assert Path(r["results"]["OUTPUT_FILLED_DEM"]).exists()


@requires_qgis
def test_grass_standard_parameters_are_not_mistaken_for_unknown(tmp_path):
    """`GRASS_REGION_PARAMETER` & co. are in the schema — they must not trigger."""
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    write_slope_dtm(cache / "dem.tif")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"](
        "grass:r.watershed",
        {"elevation": "dem.tif", "threshold": 100, "-a": True,
         "GRASS_REGION_CELLSIZE_PARAMETER": 0, "accumulation": "acc.tif"},
    )
    assert r["ok"], r.get("error")


@requires_qgis
def test_a_returned_output_path_that_was_never_written_is_an_error(tmp_path):
    """The best-hidden silent success: `ok: true` **with** a plausible path.

    Measured 2026-09-04: `gdal:rastercalculator` reported success three times with
    `OUTPUT: .../tegernheim_sinks.tif` and never wrote the file — `A-B` over two rasters
    whose grids do not align (4038x5489 against 4017x5491, edges ~14 m apart); GDAL exits
    with 0 and writes nothing. The model then took the missing file for a *path* problem
    and spent fifteen minutes on `os.getcwd()` and `resolve_path` probes.
    """
    import rasterio
    from rasterio.transform import from_origin

    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    # Two rasters with a deliberately shifted grid — the measured case.
    for name, origin, size in (("a.tif", (700000, 5400000), 40), ("b.tif", (700014, 5399993), 38)):
        with rasterio.open(
            cache / name, "w", driver="GTiff", height=size, width=size, count=1,
            dtype="float32", crs="EPSG:25832",
            transform=from_origin(origin[0], origin[1], 1, 1),
        ) as ds:
            ds.write(np.zeros((size, size), dtype="float32"), 1)

    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"](algorithm_id="gdal:rastercalculator", parameters={
        "INPUT_A": "a.tif", "BAND_A": 1, "INPUT_B": "b.tif", "BAND_B": 1,
        "FORMULA": "A-B", "OUTPUT": "out.tif"})
    assert r["ok"] is False, "ein nicht geschriebenes Ergebnis darf kein Erfolg sein"
    assert "did not write" in r["error"]
    # The message must lead the model off the wrong track, not just name the error.
    assert "NOT a path problem" in r["error"]
    assert not (cache / "out.tif").exists()


@requires_qgis
def test_matching_grids_still_produce_a_file(tmp_path):
    """Counter-check — the guard must not catch the valid call."""
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    write_slope_dtm(cache / "a.tif")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_run"](algorithm_id="gdal:rastercalculator", parameters={
        "INPUT_A": "a.tif", "BAND_A": 1, "INPUT_B": "a.tif", "BAND_B": 1,
        "FORMULA": "A-B", "OUTPUT": "zero.tif"})
    assert r["ok"], r.get("error")
    assert (cache / "zero.tif").exists()


def test_the_instructions_demand_filling_before_flow():
    """The order stands in the standing prompt, not only in the skill.

    Measured 2026-09-04: the first run that chose the method right on its own computed
    `grass:r.flow` at 20:55:16 on the **raw** DGM1 and filled the sinks only at 20:56:28 —
    the order reversed, so the flow paths end at every artefact. The `terrain-analysis`
    skill has said so since the same day, but `load_capability` stayed at zero in this
    run too; a rule that stands only in the unloaded recipe has no effect.
    """
    text = QgisToolboxCapability(workspace=".").get_instructions()(None)
    assert "fill the sinks BEFORE" in text
    assert "OUTPUT_FILLED_DEM" in text
    # The point is the order — filling must come before accumulating.
    assert text.index("fillsinkswangliu") < text.index("grass:r.flow")
