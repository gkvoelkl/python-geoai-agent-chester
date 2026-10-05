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

# Diese Datei prueft die **QGIS-Faehigkeit**. Ist QGIS abgeschaltet
# (`geodata.use_qgis: false`) oder nicht installiert, wird sie von
# `agent_build.geo_capabilities()` gar nicht erst verdrahtet — dann gibt es hier
# nichts zu pruefen. Bis zum 2026-09-06 fielen vierzehn dieser Tests im QGIS-losen
# Modus durch statt zu ueberspringen: Sie pruefen zwar reine Argumentvalidierung,
# aber die Werkzeuge holen ihr Parameterschema ueber `qgis_describe`, bevor sie die
# eigenen Argumente ansehen. Die geopandas-Seite deckt dieselben Zusicherungen in
# `test_geoops.py`/`test_rasterops.py`/`test_networkops.py` ab.
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
    """Gegenprobe: Der Guard darf den richtigen Aufruf nicht mitnehmen."""
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
    r = tools["qgis_run"](algorithm_id="gdal:rastercalculator", parameters={
        "INPUT_A": "a.tif", "BAND_A": 1, "INPUT_B": "b.tif", "BAND_B": 1,
        "FORMULA": "A-B", "OUTPUT": "out.tif"})
    assert r["ok"] is False, "ein nicht geschriebenes Ergebnis darf kein Erfolg sein"
    assert "did not write" in r["error"]
    # Die Meldung muss das Modell vom Holzweg abbringen, nicht nur den Fehler nennen.
    assert "NOT a path problem" in r["error"]
    assert not (cache / "out.tif").exists()


@requires_qgis
def test_matching_grids_still_produce_a_file(tmp_path):
    """Gegenprobe — der Guard darf den gültigen Aufruf nicht mitnehmen."""
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
