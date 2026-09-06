"""QGIS capability tests: geometric correctness + workspace path resolution."""

import math
from pathlib import Path

import numpy as np
from _util import (
    requires_qgis,
    tools_of,
    write_building_sample,
    write_point,
    write_slope_dtm,
)

from chester.capabilities.qgis import QgisToolboxCapability


@requires_qgis
def test_buffer_area_matches_circle(tmp_path):
    pt = write_point(tmp_path / "p.geojson", 500000, 5600000, "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_buffer"](input_path=str(pt), distance=100, output_path="buf.geojson")
    assert r["ok"]

    import geopandas as gpd

    # Outputs are confined to the GeoCache working dir (geocache/).
    area = gpd.read_file(tmp_path / "geocache" / "buf.geojson").geometry.area.sum()
    # QGIS approximates the circle with segments, so slightly under pi*r^2.
    assert 0.95 < area / (math.pi * 100**2) < 1.0


@requires_qgis
def test_sloppy_output_path_lands_in_workspace(tmp_path):
    pt = write_point(tmp_path / "p.geojson", 500000, 5600000, "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    # 'chester/workspace/...' (dropped dot) must resolve into the geocache dir.
    tools["qgis_buffer"](
        input_path=str(pt), distance=10, output_path="chester/workspace/x.geojson"
    )
    assert (tmp_path / "geocache" / "x.geojson").exists()
    assert not (tmp_path / "chester").exists()


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


def _write_streets(path):
    """A small layer with an OSM-style colon field name."""
    import geopandas as gpd
    from shapely.geometry import Point

    gpd.GeoDataFrame(
        {
            "building": ["a", "b", "c"],
            "addr:street": ["Hollerweg", "Hollerweg", "Hauptstrasse"],
            "geometry": [Point(0, 0), Point(1, 1), Point(2, 2)],
        },
        crs="EPSG:4326",
    ).to_file(path, driver="GeoJSON")


@requires_qgis
def test_extract_by_attribute_handles_colon_field(tmp_path):
    # Field-based selection must work on OSM colon names with no quoting games.
    src = tmp_path / "b.geojson"
    _write_streets(src)
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_extract_by_attribute"](
        input_path=str(src),
        field="addr:street",
        value="Hollerweg",
        output_path="hw.geojson",
    )
    assert r["ok"]

    import geopandas as gpd

    assert len(gpd.read_file(tmp_path / "geocache" / "hw.geojson")) == 2


@requires_qgis
def test_extract_by_attribute_rejects_unknown_operator(tmp_path):
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_extract_by_attribute"](
        input_path="x.geojson",
        field="f",
        value="v",
        output_path="o.geojson",
        operator="approximately",
    )
    assert r["ok"] is False and "unknown operator" in r["error"]


def test_buffer_refuses_geographic_crs(tmp_path):
    # A metric-distance buffer on a degree CRS would be 500° of arc, not 500 m —
    # qgis_buffer must refuse (before QGIS) so it can't silently produce a wrong
    # result. No qgis_process needed: the guard short-circuits.
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    write_point(tmp_path / "geocache" / "wgs84.geojson", 12.1, 49.0, "EPSG:4326")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_buffer"](
        input_path="geocache/wgs84.geojson", distance=500, output_path="buf.geojson"
    )
    assert r["ok"] is False and "geographic" in r["error"] and "degrees" in r["error"]


def _write_squares(path, crs):
    """Two axis-aligned squares (100x100 and 50x50) for exact-area assertions."""
    import geopandas as gpd
    from shapely.geometry import box

    gpd.GeoDataFrame(
        {"v": [3.0, 7.0], "geometry": [box(0, 0, 100, 100), box(0, 0, 50, 50)]},
        crs=crs,
    ).to_file(path, driver="GPKG")


def test_field_sum_totals_area_in_one_call(tmp_path):
    # The measuring gap: total polygon area without a hand-rolled
    # fieldcalculator -> statistics chain. 100^2 + 50^2 = 12500 m^2. No
    # @requires_qgis: $area/$length/field totals now run in-process (geopandas),
    # so they no longer shell out to qgis_process (which timed out at 100k+ feats).
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_squares(cache / "sq.gpkg", "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_field_sum"](input_path="geocache/sq.gpkg", formula="$area")
    assert r["ok"]
    assert abs(r["sum"] - 12500.0) < 1.0
    assert r["count"] == 2


def test_field_sum_totals_existing_field(tmp_path):
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_squares(cache / "sq.gpkg", "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_field_sum"](input_path="geocache/sq.gpkg", field="v")
    assert r["ok"] and abs(r["sum"] - 10.0) < 1e-6


def test_field_sum_missing_field_errors(tmp_path):
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_squares(cache / "sq.gpkg", "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_field_sum"](input_path="geocache/sq.gpkg", field="nope")
    assert r["ok"] is False and "not found" in r["error"]


def test_measure_layer_falls_back_for_arbitrary_expression(tmp_path):
    # measure_layer only handles field/$area/$length/$perimeter in-process; an
    # arbitrary QGIS expression returns None so qgis_field_sum defers to QGIS.
    from chester import geofacts

    p = tmp_path / "sq.gpkg"
    _write_squares(p, "EPSG:25832")
    assert geofacts.measure_layer(str(p), formula='"v" * 2') is None
    # $length on the two squares (perimeters 400 + 200) is handled in-process.
    length = geofacts.measure_layer(str(p), formula="$length")
    assert abs(length["sum"] - 600.0) < 1e-6


def test_field_sum_reads_the_quoted_geometry_variable(tmp_path):
    # In QGIS syntax `"x"` is a *field* reference, so a model that has read that
    # rule writes `"$area"` — which found no such column and returned a silent 0.0
    # over a perfectly good layer (benchmark supermarkets-within-10min-walk,
    # 2026-08-22). No layer can hold a field named $area, so the quoted form is
    # read as the geometry variable.
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_squares(cache / "sq.gpkg", "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_field_sum"](input_path="geocache/sq.gpkg", formula='"$area"')
    assert r["ok"] and abs(r["sum"] - 12500.0) < 1.0 and r["count"] == 2
    assert "warning" not in r


def test_field_sum_says_when_it_measured_nothing(tmp_path):
    # count 0 comes back as sum 0.0, and a bare 0.0 reads like an answer. It never
    # is one — the tool has to say so itself, or the model explains a zero that
    # means "no data" (the same run burned its request budget doing exactly that).
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_squares(cache / "sq.gpkg", "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    from chester import geofacts

    r = geofacts.measure_layer(str(cache / "sq.gpkg"), field="v")
    assert r["count"] == 2  # the fixture really does hold numbers
    empty = _measured_with_no_values(tools, cache)
    assert empty["ok"] and empty["count"] == 0 and empty["sum"] == 0.0
    assert "nothing was measured" in empty["warning"]


def _measured_with_no_values(tools, cache):
    """A layer whose column holds no numbers at all → count 0, sum 0.0."""
    import geopandas as gpd
    from shapely.geometry import Point

    gpd.GeoDataFrame(
        {"v": [None, None]}, geometry=[Point(0, 0), Point(1, 1)], crs="EPSG:25832"
    ).to_file(cache / "empty_values.gpkg", driver="GPKG")
    return tools["qgis_field_sum"](input_path="geocache/empty_values.gpkg", field="v")


def test_field_sum_requires_exactly_one_of_field_or_formula(tmp_path):
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    both = tools["qgis_field_sum"](input_path="x.gpkg", field="v", formula="$area")
    neither = tools["qgis_field_sum"](input_path="x.gpkg")
    assert both["ok"] is False and "exactly one" in both["error"]
    assert neither["ok"] is False and "exactly one" in neither["error"]


def test_field_sum_refuses_area_on_geographic_crs(tmp_path):
    # $area on a degree CRS would total square degrees, not m^2 — refuse before
    # QGIS runs (the guard short-circuits, so no qgis_process needed).
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_squares(cache / "sq4326.gpkg", "EPSG:4326")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_field_sum"](input_path="geocache/sq4326.gpkg", formula="$area")
    assert r["ok"] is False and "geographic" in r["error"]


def _write_grid_network(path, crs="EPSG:25832", extent=3000, step=200):
    """A properly-noded grid of ``step``-metre segments (routable network)."""
    import geopandas as gpd
    from shapely.geometry import LineString

    segs = []
    coords = list(range(0, extent + 1, step))
    for x in coords:
        for y in coords:
            if x + step <= extent:
                segs.append(LineString([(x, y), (x + step, y)]))
            if y + step <= extent:
                segs.append(LineString([(x, y), (x, y + step)]))
    gpd.GeoDataFrame(geometry=segs, crs=crs).to_file(path, driver="GPKG")


@requires_qgis
def test_service_area_isochrone_grows_with_speed(tmp_path):
    # Network travel-time reach: a 15-min walk isochrone is a real polygon, and a
    # bike isochrone (higher speed) reaches much further than a walk one.
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_grid_network(cache / "net.gpkg")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))

    walk = tools["qgis_service_area"](
        network_path="geocache/net.gpkg", start_lon=1500, start_lat=1500,
        start_crs="EPSG:25832", minutes=15, output_path="walk.gpkg", mode="walk",
    )
    assert walk["ok"] and walk["reach_distance_m"] == 1125  # 15/60 * 4.5 km/h * 1000

    import geopandas as gpd

    wpoly = gpd.read_file(tmp_path / "geocache" / "walk.gpkg")
    assert wpoly.geometry.geom_type.iloc[0] in ("Polygon", "MultiPolygon")
    walk_area = wpoly.geometry.area.sum()
    assert walk_area > 0

    bike = tools["qgis_service_area"](
        network_path="geocache/net.gpkg", start_lon=1500, start_lat=1500,
        start_crs="EPSG:25832", minutes=15, output_path="bike.gpkg", mode="bike",
    )
    assert bike["ok"]
    bike_area = gpd.read_file(tmp_path / "geocache" / "bike.gpkg").geometry.area.sum()
    assert bike_area > walk_area * 2  # 15 km/h reaches far more than 4.5 km/h


def test_service_area_rejects_geographic_crs(tmp_path):
    # A travel distance on a degree CRS would be measured in degrees — refuse
    # before QGIS runs (no qgis_process needed).
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_grid_network(cache / "net4326.gpkg", crs="EPSG:4326", extent=1, step=1)
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_service_area"](
        network_path="geocache/net4326.gpkg", start_lon=0, start_lat=0, minutes=15,
        output_path="x.gpkg",
    )
    assert r["ok"] is False and "geographic" in r["error"]


def _write_regensburg_network(path):
    """A small routable grid in EPSG:25832 placed at real Regensburg coordinates
    (~easting 725 km, northing 5432 km), so a WGS84 start can be checked against
    a realistically-located network."""
    import geopandas as gpd
    from shapely.geometry import LineString

    ox, oy = 725000, 5432000
    segs = []
    coords = list(range(0, 1001, 200))
    for x in coords:
        for y in coords:
            if x + 200 <= 1000:
                segs.append(LineString([(ox + x, oy + y), (ox + x + 200, oy + y)]))
            if y + 200 <= 1000:
                segs.append(LineString([(ox + x, oy + y), (ox + x, oy + y + 200)]))
    gpd.GeoDataFrame(geometry=segs, crs="EPSG:25832").to_file(path, driver="GPKG")


def test_service_area_rejects_swapped_start(tmp_path):
    # The classic lat,lon swap: geocode returns the centroid as [lon, lat] =
    # [12.0975, 49.0193], but a mis-order passes start_lon=49.0193,
    # start_lat=12.0975. That projects thousands of km from the network — the
    # guardrail must refuse it up front (no qgis_process needed) with a clear
    # message, not let QGIS emit "Point is too far from the network layer".
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_regensburg_network(cache / "net.gpkg")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_service_area"](
        network_path="geocache/net.gpkg",
        start_lon=49.019295, start_lat=12.097515,  # swapped!
        minutes=10, output_path="x.gpkg",
    )
    assert r["ok"] is False and "from the network" in r["error"]


def test_service_area_rejects_unknown_mode(tmp_path):
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_grid_network(cache / "net.gpkg")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_service_area"](
        network_path="geocache/net.gpkg", start_lon=1500, start_lat=1500,
        start_crs="EPSG:25832", minutes=15, output_path="x.gpkg", mode="teleport",
    )
    assert r["ok"] is False and "unknown mode" in r["error"]


@requires_qgis
def test_reproject_dispatches_raster_to_gdal_warp(tmp_path):
    # A raster (.tif) must reproject via gdal:warpreproject — the vector
    # native:reprojectlayer cannot load a raster (the DEM-workflow bug).
    dtm = write_slope_dtm(tmp_path / "dtm.tif")  # EPSG:25832
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_reproject"](
        input_path=str(dtm), target_crs="EPSG:4326", output_path="dtm_4326.tif"
    )
    assert r["ok"] and r["id"] == "gdal:warpreproject"

    import rasterio

    with rasterio.open(tmp_path / "geocache" / "dtm_4326.tif") as ds:
        assert ds.crs.to_epsg() == 4326


@requires_qgis
def test_reproject_dispatches_vector_to_native(tmp_path):
    pt = write_point(tmp_path / "p.geojson", 500000, 5600000, "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_reproject"](
        input_path=str(pt), target_crs="EPSG:4326", output_path="p_4326.geojson"
    )
    assert r["ok"] and r["id"] == "native:reprojectlayer"


@requires_qgis
def test_reproject_names_the_target_crs(tmp_path):
    """The code alone leaves the answer guessing — and it guesses wrong.

    In `road-impact-greenspace-100m` (2026-08-26) the run reported EPSG:25832 to
    the reader as "das Gauß-Krüger-System". It is ETRS89 / UTM zone 32N. The name
    is in every PROJ database; it just was not in the return value.
    """
    pt = write_point(tmp_path / "p.geojson", 12.1, 49.0, "EPSG:4326")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_reproject"](
        input_path=str(pt), target_crs="EPSG:25832", output_path="p_25832.geojson"
    )
    assert r["ok"]
    assert r["target_crs_name"] == "ETRS89 / UTM zone 32N"


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


@requires_qgis
def test_clip_to_donut_boundary_excludes_enclave(tmp_path):
    """qgis_clip honours a hole in the overlay polygon: a Landkreis is a ring
    around a kreisfreie Stadt (an interior ring). A building inside that hole
    (the enclave) is dropped, a building in the ring is kept, one outside is
    dropped — so a bbox overcount AND the enclave are removed in one clip."""
    import geopandas as gpd
    from shapely.geometry import Point, Polygon

    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    crs = "EPSG:25832"
    # A 300x300 square (the Kreis) with a central 100x100 hole (the Stadt).
    outer = [(0, 0), (300, 0), (300, 300), (0, 300)]
    hole = [(100, 100), (200, 100), (200, 200), (100, 200)]
    gpd.GeoDataFrame(
        {"name": ["Landkreis"]}, geometry=[Polygon(outer, [hole])], crs=crs,
    ).to_file(cache / "boundary.gpkg", driver="GPKG")
    gpd.GeoDataFrame(
        {"where": ["ring", "enclave", "outside"]},
        geometry=[Point(50, 50), Point(150, 150), Point(400, 400)],
        crs=crs,
    ).to_file(cache / "buildings.gpkg", driver="GPKG")

    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_clip"](
        input_path="geocache/buildings.gpkg",
        overlay_path="geocache/boundary.gpkg",
        output_path="clipped.gpkg",
    )
    assert r["ok"], r.get("error")
    clipped = gpd.read_file(cache / "clipped.gpkg")
    # Only the ring building survives; enclave (hole) and outside are dropped.
    assert list(clipped["where"]) == ["ring"]


def test_add_field_writes_one_computed_column(tmp_path):
    """Creating a field is the detour that cost three runs four to eleven calls.

    `QgsField` + a Qt type enum + `QgsVectorFileWriter` is the PyQGIS route, and
    that API moved between QGIS 3 and 4 — where most training data still lives.
    2026-08-24 (`mean-building-vertices`): four calls and seven minutes on
    `QgsVectorFileWriter.SaveOptions`, an attribute QGIS 4 no longer has, for a
    value the run had already computed.
    """
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_squares(cache / "sq.gpkg", "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_add_field"](
        input_path="geocache/sq.gpkg", output_path="geocache/with_area.gpkg",
        name="a", expression="$area",
    )
    assert r["ok"]
    import geopandas as gpd

    g = gpd.read_file(cache / "with_area.gpkg")
    assert "a" in g.columns
    assert abs(g["a"].sum() - 12500.0) < 1.0  # 100² + 50²


def test_add_field_rejects_an_unknown_type(tmp_path):
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_add_field"](
        input_path="x.gpkg", output_path="y.gpkg", name="a",
        expression="$area", field_type="quantum",
    )
    assert r["ok"] is False and "quantum" in r["error"] and "double" in r["error"]


def test_add_field_integer_type_lands_as_an_integer(tmp_path):
    """The FIELD_TYPE enum is positional; a wrong code silently yields the wrong
    column type, so the mapping is checked against a real write."""
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    _write_squares(cache / "sq.gpkg", "EPSG:25832")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_add_field"](
        input_path="geocache/sq.gpkg", output_path="geocache/counted.gpkg",
        name="n", expression="num_points($geometry)", field_type="integer",
    )
    assert r["ok"]
    import geopandas as gpd

    g = gpd.read_file(cache / "counted.gpkg")
    assert g["n"].dtype.kind == "i", "integer muss als Ganzzahl-Spalte landen"
    assert (g["n"] == 5).all(), "ein geschlossenes Rechteck speichert 5 Stuetzpunkte"


@requires_qgis
def test_rasterize_writes_a_raster_with_content(tmp_path):
    """Vektor → GeoTIFF in einem Aufruf — der Weg, den der Vorfall nicht fand.

    Am 2026-08-27 baute ein Lauf die Punktebene und ihr Raster über dreißig
    PyQGIS-Schnipsel von Hand und lieferte am Ende ein 266-MB-Bild aus lauter
    Nullen. `gdal:rasterize` ist der erste Treffer von `qgis_search("rasterize")`
    und war die ganze Zeit da.
    """
    import rasterio

    sample = write_building_sample(tmp_path)
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_rasterize"](
        input_path=str(sample["buildings"]), output_path="mask.tif", resolution=2
    )
    assert r["ok"] and r["crs"] == "EPSG:25832"
    with rasterio.open(tmp_path / "geocache" / "mask.tif") as src:
        values = set(src.read(1).flatten().tolist())
    assert values == {0.0, 1.0}  # Hintergrund und Brennwert, also wirklich gebrannt


@requires_qgis
def test_rasterize_refuses_to_return_an_empty_raster(tmp_path):
    """Ein Brennwert, der nichts hinterlässt, ergibt eine schwarze Fläche.

    Genau das Ergebnis des Vorfalls — hier meldet es das Werkzeug selbst, statt es
    als Datei zurückzugeben, und nennt den Ausweg.
    """
    sample = write_building_sample(tmp_path)
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_rasterize"](
        input_path=str(sample["buildings"]), output_path="leer.tif", resolution=2, burn=0
    )
    assert r["ok"] is False
    assert "empty" in r["error"]
    assert "qgis_buffer" in r["hint"] and "finer" in r["hint"]


@requires_qgis
def test_rasterize_refuses_a_geographic_layer(tmp_path):
    """`resolution=10` auf einer 4326-Ebene wären zehn **Grad** je Pixel."""
    import geopandas as gpd
    from shapely.geometry import box

    path = tmp_path / "wgs.gpkg"
    gpd.GeoDataFrame({"n": [1]}, geometry=[box(12.0, 49.0, 12.01, 49.01)],
                     crs="EPSG:4326").to_file(path)
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_rasterize"](input_path=str(path), output_path="x.tif", resolution=10)
    assert r["ok"] is False and "DEGREES" in r["error"] and "qgis_reproject" in r["error"]


def test_qgis_run_survives_a_table_without_geometry(tmp_path):
    """Eine CSV hat keine Geometrie — die Diagnose darf daran nicht zerbrechen.

    Gefunden 2026-09-01 beim Nachfahren des Dialogfalls: `native:createpointslayer
    fromtable` ist der kanonische Weg von geokodierten Adressen zu einer Punktebene,
    und er warf `AttributeError: 'DataFrame' object has no attribute 'geom_type'` —
    der Zugriff stand als einzige Zeile außerhalb des try, dessen Kommentar
    „a diagnostic must never break the run" lautet. Für den Agenten sah der richtige
    Weg damit unmöglich aus; er wich auf handgeschriebenes PyQGIS aus (37 Aufrufe,
    keine Karte).
    """
    from chester.capabilities.qgis import _geometry_types

    csv = tmp_path / "adressen.csv"
    csv.write_text("name,lon,lat\nDomplatz 7,12.096918,49.019369\n", encoding="utf-8")
    assert _geometry_types(str(csv)) is None  # kein Absturz, keine Warnung


@requires_qgis
def test_points_from_a_table_is_one_call(tmp_path):
    """Der ganze Weg: Tabelle rein, Punktebene raus — ohne eine Zeile PyQGIS."""
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


@requires_qgis
def test_clip_accepts_a_raster_and_crops_it(tmp_path):
    """`qgis_clip` muss auch ein Raster schneiden — ein Intent, zwei Algorithmen.

    `native:clip` ist vektor-only und antwortet auf ein GeoTIFF mit "Could not
    load source layer for INPUT: … .tif not found" — eine Typverwechslung in den
    Worten einer fehlenden Datei. Zweimal gemessen (2026-09-01/02) beim Schneiden
    eines DGM1 auf eine Gemeindegrenze: einmal vier Zusatzaufrufe, um
    `gdal:cliprasterbymasklayer` und seinen `MASK`-Parameter zu finden, einmal ein
    entgleister Lauf mit 1084 s Zeitdeckel. „Benannte Fläche → an der Grenze
    clippen" ist eine harte Regel dieses Projekts; der Schritt darf nicht daran
    hängen, welcher Algorithmus welche Geometrieart nimmt.
    """
    import geopandas as gpd
    import rasterio
    from shapely.geometry import box

    dtm = write_slope_dtm(tmp_path / "dtm.tif")
    with rasterio.open(dtm) as src:
        w, s, e, n = src.bounds
        full_crs = src.crs
    # Maske auf das mittlere Viertel — so ist "geschnitten" an der Ausdehnung ablesbar.
    mask = tmp_path / "mask.geojson"
    gpd.GeoDataFrame(
        geometry=[box(w + (e - w) / 4, s + (n - s) / 4, e - (e - w) / 4, n - (n - s) / 4)],
        crs=full_crs,
    ).to_file(mask)

    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_clip"](input_path=str(dtm), overlay_path=str(mask),
                           output_path="dtm_clipped.tif")

    assert r["ok"], r
    with rasterio.open(tmp_path / "geocache" / "dtm_clipped.tif") as out:
        assert out.width < src.width and out.height < src.height, "nicht zugeschnitten"
        assert out.crs == full_crs


@requires_qgis
def test_clip_still_clips_vectors(tmp_path):
    """Der Vektorweg darf durch die Raster-Weiche nicht verlorengehen."""
    import geopandas as gpd
    from shapely.geometry import box

    pts = tmp_path / "pts.geojson"
    gpd.GeoDataFrame(
        geometry=gpd.points_from_xy([500_000, 500_500, 600_000], [5_600_000] * 3),
        crs="EPSG:25832",
    ).to_file(pts)
    mask = tmp_path / "mask.geojson"
    gpd.GeoDataFrame(geometry=[box(499_900, 5_599_900, 500_600, 5_600_100)],
                     crs="EPSG:25832").to_file(mask)

    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_clip"](input_path=str(pts), overlay_path=str(mask),
                           output_path="pts_clipped.geojson")

    assert r["ok"], r
    kept = gpd.read_file(tmp_path / "geocache" / "pts_clipped.geojson")
    assert len(kept) == 2, "der Punkt bei 600 km muss draussen sein"


# ── Pfadparameter aus dem Algorithmus-Schema ────────────────────────────
#
# `_PATH_KEYS` ist eine handgepflegte Namensliste, und ihr eigener Kommentar hält
# zwei verlorene Läufe fest (POLYGONS 2026-08-19, RASTERCOPY 2026-08-23). GRASS
# macht den Ansatz unhaltbar statt bloß riskant: alle 307 Algorithmen benennen ihre
# Parameter klein. Seit 2026-09-03 leitet `_resolve_params` die Pfadparameter
# deshalb aus `qgis_describe` ab; die Namensliste bleibt nur als Rückfallebene.


@requires_qgis
def test_lowercase_grass_output_lands_in_the_workspace(tmp_path):
    """Der Fall, der einen Dialogzug gekostet hat (2026-09-03).

    `grass:r.watershed` nennt seine Ausgabe `accumulation` — kleingeschrieben und
    in keiner Namensliste. Unaufgelöst schrieb qgis_process die Datei ins
    Arbeitsverzeichnis des Prozesses (das Repo-Wurzelverzeichnis), meldete dabei
    `ok: true` und gab den bloßen Namen zurück. Der Agent suchte danach eine Datei,
    die nie dort lag, wo er sah.
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
    """`native:fillsinkswangliu` heißt sein Ergebnis `OUTPUT_FILLED_DEM`.

    Auch das fehlte in der Namensliste — die traf nur `OUTPUT` exakt. Der Fall
    zeigt, dass es nicht bloß um GRASS geht.
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
    """Die Sidecar-Regel gilt für den neuen Pfad genauso — sonst wäre die
    Auflösung repariert und die Herkunft verloren."""
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


# ── stille Erfolge ──────────────────────────────────────────────────────
#
# QGIS ignoriert unbekannte Parameter wortlos. Gemessen 2026-09-04 im laufenden
# Dialog: `native:fillsinkswangliu` mit `OUTPUT` (der Algorithmus heißt seine Ausgabe
# `OUTPUT_FILLED_DEM`) lieferte `ok: true` mit `results: {}` und schrieb nichts. Der
# Folgeschritt scheiterte an der Datei, die nie entstand; die Erholung dauerte fünf
# Minuten und endete auf einem Überbleibsel aus einem früheren Lauf — klaglos
# verrechnet. Ein stiller Erfolg ist die schlimmste Fehlerform.


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
    # Die Meldung muss den Ausweg nennen, nicht nur den Fehler.
    assert "OUTPUT_FILLED_DEM" in r["error"]
    assert "no parameter" in r["error"]


@requires_qgis
def test_the_correctly_named_parameter_still_works(tmp_path):
    """Gegenprobe: Der Wächter darf den richtigen Aufruf nicht mitnehmen."""
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
    """`GRASS_REGION_PARAMETER` & Co. stehen im Schema — sie dürfen nicht anschlagen."""
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
    """Der bestversteckte stille Erfolg: `ok: true` **mit** plausiblem Pfad.

    Gemessen 2026-09-04: `gdal:rastercalculator` meldete dreimal Erfolg mit
    `OUTPUT: .../tegernheim_sinks.tif` und schrieb die Datei nie — `A-B` über zwei
    Raster, deren Gitter nicht deckungsgleich sind (4038x5489 gegen 4017x5491, Ränder
    um ~14 m versetzt); GDAL beendet sich mit 0 und schreibt nichts. Das Modell hielt
    die fehlende Datei daraufhin für ein *Pfad*problem und verbrachte fünfzehn Minuten
    mit `os.getcwd()`- und `resolve_path`-Sonden.
    """
    import rasterio
    from rasterio.transform import from_origin

    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    # Zwei Raster mit absichtlich verschobenem Gitter — der gemessene Fall.
    for name, origin, size in (("a.tif", (700000, 5400000), 40), ("b.tif", (700014, 5399993), 38)):
        with rasterio.open(
            cache / name, "w", driver="GTiff", height=size, width=size, count=1,
            dtype="float32", crs="EPSG:25832",
            transform=from_origin(origin[0], origin[1], 1, 1),
        ) as ds:
            ds.write(np.zeros((size, size), dtype="float32"), 1)

    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_raster_calc"](
        input_a="a.tif", input_b="b.tif", formula="A-B", output_path="out.tif",
        band_a=1, band_b=1,
    )
    assert r["ok"] is False, "ein nicht geschriebenes Ergebnis darf kein Erfolg sein"
    assert "did not write" in r["error"]
    # Die Meldung muss das Modell vom Holzweg abbringen, nicht nur den Fehler nennen.
    assert "NOT a path problem" in r["error"]
    assert not (cache / "out.tif").exists()


@requires_qgis
def test_matching_grids_still_produce_a_file(tmp_path):
    """Gegenprobe — der Wächter darf den gültigen Aufruf nicht mitnehmen."""
    cache = tmp_path / "geocache"
    cache.mkdir(parents=True, exist_ok=True)
    write_slope_dtm(cache / "a.tif")
    tools = tools_of(QgisToolboxCapability(workspace=str(tmp_path)))
    r = tools["qgis_raster_calc"](
        input_a="a.tif", input_b="a.tif", formula="A-B", output_path="zero.tif",
        band_a=1, band_b=1,
    )
    assert r["ok"], r.get("error")
    assert (cache / "zero.tif").exists()


def test_the_instructions_demand_filling_before_flow():
    """Die Reihenfolge steht im Dauerprompt, nicht nur im Skill.

    Gemessen 2026-09-04: Der erste Lauf, der die Methode von selbst richtig wählte,
    rechnete `grass:r.flow` um 20:55:16 auf dem **rohen** DGM1 und füllte die Senken
    erst um 20:56:28 — die Reihenfolge umgedreht, die Fließwege enden damit an jedem
    Artefakt. Der `terrain-analysis`-Skill sagt es seit demselben Tag, aber
    `load_capability` blieb auch in diesem Lauf bei null; eine Regel, die nur im
    ungeladenen Rezept steht, wirkt nicht.
    """
    text = QgisToolboxCapability(workspace=".").get_instructions()(None)
    assert "fill the sinks BEFORE" in text
    assert "OUTPUT_FILLED_DEM" in text
    # Der Zweck ist die Reihenfolge — Füllen muss vor dem Akkumulieren stehen.
    assert text.index("fillsinkswangliu") < text.index("grass:r.flow")
