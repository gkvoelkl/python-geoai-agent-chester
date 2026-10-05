"""The eleven vector operations on GeoPandas (`chester/geoops.py`) — offline, without QGIS.

Phase KQ step 2. What is checked is not that geopandas can compute, but that the traps
this project paid for are encoded: metric work in a geographic CRS, an empty result from
non-empty input, and the difference between selecting and clipping.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Point, box

from chester import geoops


def _ws(tmp_path):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    return str(tmp_path)


def _layer(tmp_path, name, geoms, crs="EPSG:25832", **cols):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    data = cols or {"x": list(range(len(geoms)))}
    gpd.GeoDataFrame(data, geometry=geoms, crs=crs).to_file(
        tmp_path / "geocache" / f"{name}.gpkg"
    )
    return f"{name}.gpkg"


def test_all_eleven_operations_are_exported():
    """One directory, so tools and sandbox do not drift apart."""
    assert set(geoops.OPERATIONS) == {
        "reproject", "buffer", "clip", "intersection", "extract_by_location",
        "extract_by_attribute", "dissolve", "merge", "join", "add_field", "field_sum",
    }


def test_every_operation_is_also_a_tool(tmp_path):
    """The coupling itself, not just the list.

    The finding of 2026-09-07: what stands only in the sandbox namespace the model does
    not use. So no operation may land there without being in the tool catalogue — this
    test fails as soon as someone extends `geoops` and forgets `vectorops`.
    """
    from chester.vectoroptools import build_tools

    tools = {t.__name__ for t in build_tools(str(tmp_path))}
    assert tools == {f"vector_{name}" for name in geoops.OPERATIONS}


def test_buffer_refuses_a_geographic_crs(tmp_path):
    """500 in degrees is about 55,000 km — that must not become a result.

    The oldest trap of the bank (`buffer-in-degrees`). A buffer in WGS84 gives a shape
    that looks plausible and is wrong by orders of magnitude; refusing is the only honest
    answer.
    """
    src = _layer(tmp_path, "pts", [Point(12.09, 49.01)], crs="EPSG:4326")
    res = geoops.buffer(src, "out.gpkg", 500, workspace=_ws(tmp_path))
    assert res["ok"] is False
    assert "geographic CRS" in res["error"] and "DEGREES" in res["error"]
    assert "25832" in res["error"], "die Absage muss den Ausweg nennen"


def test_clip_keeps_every_geometry_type(tmp_path):
    """The difference to `native:clip`, in one test.

    Measured 2026-09-05: QGIS's clip writes ONE geometry type — the one the file header
    declares — and left 18 of 246 supermarkets, because the 138 polygons dropped out
    without a word. Through geopandas both families are kept.
    """
    src = _layer(tmp_path, "mixed", [Point(1, 1), box(0, 0, 4, 4), Point(100, 100)])
    mask = _layer(tmp_path, "mask", [box(-1, -1, 10, 10)])
    res = geoops.clip(src, mask, "clipped.gpkg", workspace=_ws(tmp_path))
    assert res["ok"] is True and res["features_out"] == 2
    got = gpd.read_file(tmp_path / "geocache" / "clipped.gpkg")
    assert set(got.geom_type) == {"Point", "Polygon"}, "ein Typ ging verloren"


def test_an_empty_result_says_so(tmp_path):
    """Full in, empty out — the project's costliest silent success."""
    src = _layer(tmp_path, "here", [box(0, 0, 4, 4)])
    far = _layer(tmp_path, "far", [box(1000, 1000, 1004, 1004)])
    res = geoops.clip(src, far, "empty.gpkg", workspace=_ws(tmp_path))
    assert res["ok"] is True and res["features_out"] == 0
    assert "output is EMPTY" in res["warning"]
    assert "do not build on it" in res["warning"]


def test_extract_by_location_keeps_whole_features(tmp_path):
    """Selecting is not clipping — the probe `intersection-not-selection`.

    A polygon that only touches the mask comes through **whole**; `clip` would have cut it
    at the edge. Confusing the two gives areas that are off many times over.
    """
    src = _layer(tmp_path, "polys", [box(0, 0, 10, 10)])
    mask = _layer(tmp_path, "small", [box(0, 0, 2, 2)])
    ws = _ws(tmp_path)
    sel = geoops.extract_by_location(src, mask, "sel.gpkg", workspace=ws)
    cut = geoops.clip(src, mask, "cut.gpkg", workspace=ws)
    a_sel = gpd.read_file(tmp_path / "geocache" / "sel.gpkg").geometry.area.sum()
    a_cut = gpd.read_file(tmp_path / "geocache" / "cut.gpkg").geometry.area.sum()
    assert sel["features_out"] == cut["features_out"] == 1
    assert a_sel == 100 and a_cut == 4, "Auswahl behaelt 100 m², Schnitt behaelt 4 m²"


def test_area_needs_a_metric_crs(tmp_path):
    """Square degrees are not square metres — checked in both tools."""
    src = _layer(tmp_path, "geo", [box(12.0, 49.0, 12.1, 49.1)], crs="EPSG:4326")
    ws = _ws(tmp_path)
    summed = geoops.field_sum(src, "area", workspace=ws)
    added = geoops.add_field(src, "out.gpkg", "a", "area", workspace=ws)
    assert summed["ok"] is False and "geographic CRS" in summed["error"]
    assert added["ok"] is False and "square degrees" in added["error"]


def test_field_sum_answers_with_the_number(tmp_path):
    """A sum is an answer, not a file — and it names its CRS along with it."""
    src = _layer(tmp_path, "sq", [box(0, 0, 10, 10), box(20, 20, 30, 30)])
    res = geoops.field_sum(src, "area", workspace=_ws(tmp_path))
    assert res["ok"] is True and res["sum"] == 200.0
    assert res["features"] == 2 and res["crs"] == "EPSG:25832"


def test_a_non_numeric_column_is_refused(tmp_path):
    """A sum over text has no meaning — that should be said, not guessed."""
    src = _layer(tmp_path, "named", [Point(1, 1), Point(2, 2)], label=["a", "b"])
    res = geoops.field_sum(src, "label", workspace=_ws(tmp_path))
    assert res["ok"] is False and "not numbers" in res["error"]


def test_every_written_output_carries_provenance(tmp_path):
    """Every writing operation stamps its sidecar — the project's rule."""
    import os

    src = _layer(tmp_path, "src", [box(0, 0, 4, 4)])
    res = geoops.reproject(src, "wgs.gpkg", "EPSG:4326", workspace=_ws(tmp_path))
    assert res["ok"] is True
    assert os.path.isfile(res["output"] + ".meta.json")


def test_a_mistyped_layer_name_is_a_refusal_not_a_crash(tmp_path):
    """Measured 2026-09-07 (`supermarket-accessibility-choropleth`): one word off.

    The model wrote `regress_supermarkets_split_polygon.gpkg` instead of `regensburg_…`.
    `gpd.read_file` raised `DataSourceError`, the exception left the tool and **ended the
    run** after 930 s — over a typo. All eleven operations shared this route.
    """
    have = _layer(tmp_path, "regensburg_supermarkets", [Point(1, 1)])
    for call in (
        lambda: geoops.reproject("regress_supermarkets.gpkg", "o.gpkg", "EPSG:4326",
                                 workspace=_ws(tmp_path)),
        lambda: geoops.buffer("regress_supermarkets.gpkg", "o.gpkg", 10,
                              workspace=_ws(tmp_path)),
        lambda: geoops.clip(have, "regress_supermarkets.gpkg", "o.gpkg",
                            workspace=_ws(tmp_path)),
        lambda: geoops.merge([have, "regress_supermarkets.gpkg"], "o.gpkg",
                             workspace=_ws(tmp_path)),
    ):
        res = call()
        assert res["ok"] is False, "eine Absage, kein Absturz"
        assert "nothing was read and nothing written" in res["error"]
        assert have in res["did_you_mean"], "und der Rückgabekanal nennt den Nachbarn"


def test_the_hint_falls_back_to_what_is_there(tmp_path):
    """Without a similar name: better show what exists than nothing at all."""
    _layer(tmp_path, "gruenflaechen", [Point(1, 1)])
    res = geoops.buffer("voellig_anderes.gpkg", "o.gpkg", 10, workspace=_ws(tmp_path))
    assert res["ok"] is False
    assert "gruenflaechen.gpkg" in res["did_you_mean"]


def test_intersection_survives_a_mixed_geometry_layer(tmp_path):
    """OSM supermarkets are mixed by nature: shops mapped as nodes come back as
    points, shops mapped as buildings as polygons.

    `gpd.overlay` refuses such an input outright (``NotImplementedError: df1 contains
    mixed geometry types``), and on 2026-09-21 that exception took a whole ressort run
    with it: 292 s of work lost, then 149 s in which the orchestrator worked out for
    itself what the tool already knew and converted the layer to centroids. Both were
    avoidable — the operation is well defined per geometry class.

    Asserted on the result, not merely on the absence of the exception: **both** kinds
    must survive, and the return must say that two kinds were counted together.
    """
    mixed = _layer(tmp_path, "supermarkets",
                   [Point(1, 1), Point(50, 50), box(2, 2, 4, 4), box(60, 60, 62, 62)])
    mask = _layer(tmp_path, "buffer", [box(0, 0, 10, 10)])
    res = geoops.intersection(mixed, mask, "reachable.gpkg", workspace=_ws(tmp_path))

    assert res["ok"] is True
    out = gpd.read_file(tmp_path / "geocache" / "reachable.gpkg")
    kinds = set(out.geometry.geom_type.str.replace("Multi", "", regex=False))
    assert kinds == {"Point", "Polygon"}, f"a geometry class was lost: {kinds}"
    assert res["features_out"] == 2, "the point and the polygon inside the mask"
    assert res["mixed_geometry"] == ["Point", "Polygon"]
    assert "vector_split_by_geometry" in res["note"], "the way forward belongs in it"


def test_an_unmixed_layer_says_nothing_about_geometry_classes(tmp_path):
    """The note is a finding, not noise: it appears only where it applies."""
    plain = _layer(tmp_path, "plain", [box(1, 1, 2, 2), box(3, 3, 4, 4)])
    mask = _layer(tmp_path, "mask", [box(0, 0, 10, 10)])
    res = geoops.intersection(plain, mask, "cut.gpkg", workspace=_ws(tmp_path))
    assert res["ok"] and "mixed_geometry" not in res and "note" not in res


def test_no_operation_ever_raises(tmp_path):
    """The contract, not a special case.

    Paid for twice: on 2026-09-07 a missing layer took a 930 s run with it, on
    2026-09-21 a mixed geometry took a ressort run. The first time a catch for *one*
    exception was added; the second time a different one was raised. So the probe uses
    a file that holds no geodata at all — some exception from deep inside geopandas,
    and none of them may get out.
    """
    ws = _ws(tmp_path)
    junk = tmp_path / "geocache" / "kaputt.gpkg"
    junk.write_bytes(b"das ist kein GeoPackage")
    good = _layer(tmp_path, "ok", [box(0, 0, 1, 1)])

    for call in (
        lambda: geoops.intersection("kaputt.gpkg", good, "x.gpkg", workspace=ws),
        lambda: geoops.clip("kaputt.gpkg", good, "x.gpkg", workspace=ws),
        lambda: geoops.reproject("kaputt.gpkg", "x.gpkg", "EPSG:25832", workspace=ws),
        lambda: geoops.buffer("kaputt.gpkg", "x.gpkg", 10, workspace=ws),
    ):
        res = call()
        assert res["ok"] is False, res
        assert res["error"], "a failure must say what went wrong"
        assert "trying a variant" in res.get("note", ""), "and that a retry will not help"


# ── Analytic value checks ────────────────────────────────────────────────────
# The tests above encode the traps; these four encode the arithmetic. Four
# operations had no numeric expectation at all, and their failure mode is silent:
# a wrong reprojection, a dissolve that groups without merging, a join that matches
# nothing, a computed column in the wrong unit — each returns `ok: true` and a
# plausible-looking layer. Every expected number below is derived by hand.


def test_reproject_round_trips_to_the_same_coordinate(tmp_path):
    """There and back must land on the start — within a millimetre."""
    ws = _ws(tmp_path)
    x, y = 726516.06, 5434247.46          # Regensburg cathedral in EPSG:25832
    src = _layer(tmp_path, "pt", [Point(x, y)])

    assert geoops.reproject(src, "wgs.gpkg", "EPSG:4326", workspace=ws)["ok"]
    assert geoops.reproject("wgs.gpkg", "back.gpkg", "EPSG:25832", workspace=ws)["ok"]

    got = gpd.read_file(tmp_path / "geocache" / "back.gpkg").geometry[0]
    assert got.distance(Point(x, y)) < 1e-3, f"drifted {got.distance(Point(x, y))} m"

    # And the intermediate really is degrees near the cathedral, not metres.
    mid = gpd.read_file(tmp_path / "geocache" / "wgs.gpkg").geometry[0]
    assert abs(mid.x - 12.098) < 1e-3 and abs(mid.y - 49.019) < 1e-3, (mid.x, mid.y)


def test_dissolve_removes_the_shared_edge(tmp_path):
    """Two touching 10x10 squares become one 20x10 rectangle: 200 m2, 60 m perimeter.

    The perimeter is the part that matters. A dissolve that merely *groups* the two
    squares into a MultiPolygon also reports one feature and 200 m2 — and keeps the
    shared edge, so the perimeter stays 80 m. Only the length tells the two apart.
    """
    src = _layer(tmp_path, "two", [box(0, 0, 10, 10), box(10, 0, 20, 10)])
    res = geoops.dissolve(src, "one.gpkg", workspace=_ws(tmp_path))
    assert res["ok"] is True and res["features_out"] == 1, res

    got = gpd.read_file(tmp_path / "geocache" / "one.gpkg").geometry[0]
    assert got.area == 200.0, got.area
    assert got.length == 60.0, f"shared edge survived: {got.length} m instead of 60"


def test_add_field_computes_the_area_it_claims(tmp_path):
    """A 10x10 square is 100 m2 — and the column must say so, not 1e-8 square degrees."""
    src = _layer(tmp_path, "sq", [box(0, 0, 10, 10)])
    res = geoops.add_field(src, "with_area.gpkg", "a", "area", workspace=_ws(tmp_path))
    assert res["ok"] is True, res
    assert gpd.read_file(tmp_path / "geocache" / "with_area.gpkg")["a"][0] == 100.0


def test_join_matches_on_the_key_without_multiplying_rows(tmp_path):
    """Two of three polygons have a partner: 2 joined, 1 unjoined, still 3 features."""
    import pandas as pd

    ws = _ws(tmp_path)
    src = _layer(tmp_path, "areas",
                 [box(0, 0, 1, 1), box(2, 0, 3, 1), box(4, 0, 5, 1)],
                 key=["a", "b", "c"])
    table = tmp_path / "geocache" / "vals.csv"
    pd.DataFrame({"key": ["a", "b"], "value": [10, 20]}).to_csv(table, index=False)

    res = geoops.join(src, str(table), "joined.gpkg", field="key", workspace=ws)
    assert res["ok"] is True, res
    assert res["joined"] == 2 and res["unjoined"] == 1, res

    got = gpd.read_file(tmp_path / "geocache" / "joined.gpkg")
    assert len(got) == 3, f"the join multiplied rows: {len(got)}"
    # Numbers, not numerals: only the KEY is read as text. `dtype=str` over the whole
    # table used to turn `value` into "10"/"20", and `field_sum` then concatenated
    # them into 102030.0 while reporting ok (2026-09-22).
    assert list(got.sort_values("key")["value"][:2]) == [10, 20]
    assert geoops.field_sum("joined.gpkg", "value", workspace=ws)["sum"] == 30.0


def test_a_leading_zero_key_still_survives_the_join(tmp_path):
    """The narrowed dtype must not give back the trap it was there to prevent."""
    import pandas as pd

    ws = _ws(tmp_path)
    src = _layer(tmp_path, "gem", [box(0, 0, 1, 1)], key=["09375117"])
    table = tmp_path / "geocache" / "ags.csv"
    pd.DataFrame({"key": ["09375117"], "pop": [152610]}).to_csv(table, index=False)

    res = geoops.join(src, str(table), "out.gpkg", field="key", workspace=ws)
    assert res["ok"] is True and res["joined"] == 1, res
    assert geoops.field_sum("out.gpkg", "pop", workspace=ws)["sum"] == 152610.0


def test_a_text_column_of_numerals_is_refused_not_concatenated(tmp_path):
    """`"10" + "20" + "30"` is `"102030"`, and `float()` accepts it without a word.

    The old guard was a try/except around `float(series.sum())`, which only caught
    text that does not parse as a number. A column of numerals disguised as text got
    through and produced a wrong total under `ok: true`.
    """
    src = _layer(tmp_path, "txt", [box(0, 0, 1, 1), box(2, 0, 3, 1), box(4, 0, 5, 1)],
                 value=["10", "20", "30"])
    res = geoops.field_sum(src, "value", workspace=_ws(tmp_path))
    assert res["ok"] is False, f"concatenated instead of refusing: {res}"
    assert "concatenate" in res["error"]
    assert "astype(float)" in res["error"], "the refusal must name the way out"


def test_buffer_produces_the_circle_it_promises(tmp_path):
    """Four tests called `buffer` before this one and every one of them was a refusal.

    The most-used vector operation had no check on the shape it returns. The bounds
    are exact whatever the segment count; the area is the analytic circle minus the
    polygonal approximation (shapely's default 8 segments per quarter turn inscribes
    a 32-gon, which is ~0.64 % short of pi*r^2).
    """
    import math

    src = _layer(tmp_path, "pt", [Point(0, 0)])
    res = geoops.buffer(src, "buf.gpkg", 100.0, workspace=_ws(tmp_path))
    assert res["ok"] is True and res["features_out"] == 1, res

    got = gpd.read_file(tmp_path / "geocache" / "buf.gpkg").geometry[0]
    assert got.bounds == (-100.0, -100.0, 100.0, 100.0), got.bounds
    circle = math.pi * 100.0 ** 2
    assert 0.99 * circle < got.area < circle, f"{got.area} is not a 100 m circle"


def test_extract_by_attribute_keeps_exactly_the_matching_features(tmp_path):
    """The operation had no test at all — neither a value nor a contract."""
    ws = _ws(tmp_path)
    src = _layer(tmp_path, "houses", [Point(0, 0), Point(1, 1), Point(2, 2)],
                 height=[10, 20, 30])

    res = geoops.extract_by_attribute(src, "tall.gpkg", "height > 15", workspace=ws)
    assert res["ok"] is True, res
    assert res["features_in"] == 3 and res["features_out"] == 2, res
    assert sorted(gpd.read_file(tmp_path / "geocache" / "tall.gpkg")["height"]) == [20, 30]

    # A filter that matches nothing is the "voll rein, leer raus" case, not a failure.
    none = geoops.extract_by_attribute(src, "no.gpkg", "height > 99", workspace=ws)
    assert none["ok"] is True and none["features_out"] == 0
    assert "output is EMPTY" in none["warning"]


def test_merge_stacks_every_feature_and_keeps_both_columns(tmp_path):
    """2 + 3 features are 5, and neither layer's attribute disappears."""
    ws = _ws(tmp_path)
    a = _layer(tmp_path, "a", [Point(0, 0), Point(1, 1)], left=[1, 2])
    b = _layer(tmp_path, "b", [Point(2, 2), Point(3, 3), Point(4, 4)], right=[7, 8, 9])

    res = geoops.merge([a, b], "both.gpkg", workspace=ws)
    assert res["ok"] is True and res["features_out"] == 5, res

    got = gpd.read_file(tmp_path / "geocache" / "both.gpkg")
    assert len(got) == 5
    assert {"left", "right"} <= set(got.columns), sorted(got.columns)
    assert got["left"].notna().sum() == 2 and got["right"].notna().sum() == 3
