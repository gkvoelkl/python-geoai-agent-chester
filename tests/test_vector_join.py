"""`vector_join` — der QGIS-freie Attribut-Join, und die Falle, die ihn berühmt macht.

Die einundzwanzigste geprüfte Operation und die letzte Lücke im QGIS-freien Betrieb:
bis heute zeigte die Instruktion für den Statistik-Join auf
`qgis_run("native:joinattributestable")` — ein Werkzeug, das bei abgeschaltetem QGIS
nicht existiert.

Der Fehler, den er nicht verschweigen darf, hat schon eine eigene Probe
(`join-leading-zero-ags`, 2026-09-05): `native:joinattributestable` lieferte
``{"JOINED_COUNT": 0, "UNJOINABLE_COUNT": 4}`` und `qgis_run` reichte das als
``ok: true`` weiter. Die Ausgabedatei war da, hielt alle vier Gemeinden und trug die
angehängte Spalte — leer in jeder Zeile. Ein Join vergleicht **Wert und Typ**, und die
Zahl 9375117 ist nicht der Text „09375117"; jeder bayerische AGS beginnt mit der
Landesschlüssel 09, den ein als Zahl gelesener Schlüssel wegwirft.
"""

from __future__ import annotations

import geopandas as gpd
from _util import tools_of
from shapely.geometry import box

from chester.capabilities.vector import VectorCapability

#: Vier bayerische Gemeinden, Schlüssel wie im amtlichen Datensatz: mit führender Null.
_AGS = ["09375117", "09375118", "09375119", "09375120"]


def _layer(tmp_path, keys=_AGS):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    gdf = gpd.GeoDataFrame(
        {"ags": keys, "name": [f"Gemeinde {k[-3:]}" for k in keys],
         "geometry": [box(i, 0, i + 1, 1) for i in range(len(keys))]},
        crs="EPSG:25832")
    gdf.to_file(tmp_path / "geocache" / "gemeinden.gpkg")
    return "gemeinden.gpkg"


def _table(tmp_path, keys, name="stats.csv", value="einwohner"):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    lines = [f"ags,{value}"] + [f"{k},{1000 + i}" for i, k in enumerate(keys)]
    (tmp_path / "geocache" / name).write_text("\n".join(lines), encoding="utf-8")
    return name


def _join(tmp_path):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    return tools_of(VectorCapability(workspace=str(tmp_path)))["vector_join"]


def test_a_clean_join_reports_what_it_matched(tmp_path):
    out = _join(tmp_path)(input_path=_layer(tmp_path),
                          table_path=_table(tmp_path, _AGS),
                          output_path="joined.gpkg", field="ags")
    assert out["ok"] and out["joined"] == 4 and out["unjoined"] == 0
    assert out["columns_added"] == ["einwohner"]
    assert "warning" not in out
    assert gpd.read_file(out["output"])["einwohner"].notna().all()


def test_the_leading_zero_is_named_as_the_cause(tmp_path):
    """Der Fall aus der Probe: die Tabelle hat den Schlüssel als Zahl gelesen."""
    stripped = [k.lstrip("0") for k in _AGS]
    out = _join(tmp_path)(input_path=_layer(tmp_path),
                          table_path=_table(tmp_path, stripped),
                          output_path="joined.gpkg", field="ags")
    assert out["ok"], "die Datei entsteht — sie ist ja nicht kaputt, nur leer"
    assert out["joined"] == 0 and out["unjoined"] == 4
    assert "leading zeros" in out["warning"]
    assert "[7]" in out["warning"] and "[8]" in out["warning"], "die Breiten benennen"


def test_a_partial_join_is_not_silent(tmp_path):
    """Die halb gefüllte Spalte ist die gefährlichere Form: die Datei sieht fertig aus."""
    out = _join(tmp_path)(input_path=_layer(tmp_path),
                          table_path=_table(tmp_path, _AGS[:2]),
                          output_path="joined.gpkg", field="ags")
    assert out["joined"] == 2 and out["unjoined"] == 2
    assert "2 of 4" in out["warning"]
    assert "looks complete and is not" in out["warning"]


def test_differently_named_key_columns(tmp_path):
    table = _table(tmp_path, _AGS)
    text = (tmp_path / "geocache" / table).read_text().replace("ags,", "gemeindeschluessel,")
    (tmp_path / "geocache" / table).write_text(text, encoding="utf-8")
    out = _join(tmp_path)(input_path=_layer(tmp_path), table_path=table,
                          output_path="joined.gpkg", field="ags",
                          table_field="gemeindeschluessel")
    assert out["ok"] and out["joined"] == 4


def test_a_missing_key_column_names_the_alternatives(tmp_path):
    out = _join(tmp_path)(input_path=_layer(tmp_path),
                          table_path=_table(tmp_path, _AGS),
                          output_path="joined.gpkg", field="gemeindekennziffer")
    assert out["ok"] is False
    assert "gemeindekennziffer" in out["error"] and "ags" in out["error"]


def test_the_geometry_survives_the_join(tmp_path):
    """Ein Join darf die Ebene nicht in eine Tabelle verwandeln."""
    out = _join(tmp_path)(input_path=_layer(tmp_path),
                          table_path=_table(tmp_path, _AGS),
                          output_path="joined.gpkg", field="ags")
    joined = gpd.read_file(out["output"])
    assert len(joined) == 4
    assert joined.geometry.notna().all() and str(joined.crs) == "EPSG:25832"
