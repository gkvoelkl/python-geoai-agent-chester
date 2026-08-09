"""QGIS capability tests: geometric correctness + workspace path resolution."""

import math

from _util import requires_qgis, tools_of, write_point, write_slope_dtm

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
