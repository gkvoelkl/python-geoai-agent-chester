"""Tests for the QgisProcess runner: search filtering (unit) + describe/run (qgis)."""

from _util import requires_qgis, write_point

from chester.qgis_process import QgisProcess


def test_search_filters_by_keyword():
    # Build a QgisProcess without touching the real binary, inject a catalog.
    qp = QgisProcess.__new__(QgisProcess)
    qp._algorithms = {
        "native:buffer": {
            "name": "Buffer", "group": "Vector geometry",
            "short_description": "Computes a buffer area", "tags": ["buffer"],
            "provider": "native",
        },
        "native:slope": {
            "name": "Slope", "group": "Raster terrain",
            "short_description": "Slope from a DEM", "tags": ["slope"],
            "provider": "gdal",
        },
    }
    ids = [h["id"] for h in qp.search("buffer")]
    assert "native:buffer" in ids
    assert "native:slope" not in ids


def test_search_matches_tags_and_description():
    qp = QgisProcess.__new__(QgisProcess)
    qp._algorithms = {
        "native:slope": {
            "name": "Slope", "group": "g", "short_description": "steepness",
            "tags": ["gradient"], "provider": "gdal",
        }
    }
    assert [h["id"] for h in qp.search("gradient")] == ["native:slope"]
    assert [h["id"] for h in qp.search("steepness")] == ["native:slope"]


def test_search_matches_all_tokens_in_any_order():
    # Multi-word keywords match on all tokens, not as one literal phrase:
    # "join attributes location" must find "Join attributes by location".
    qp = QgisProcess.__new__(QgisProcess)
    qp._algorithms = {
        "native:joinattributesbylocation": {
            "name": "Join attributes by location", "group": "Vector general",
            "short_description": "Joins by a spatial relationship",
            "tags": ["join"], "provider": "native",
        },
        "native:buffer": {
            "name": "Buffer", "group": "g", "short_description": "buffer area",
            "tags": ["buffer"], "provider": "native",
        },
    }
    ids = [h["id"] for h in qp.search("join attributes location")]
    assert ids == ["native:joinattributesbylocation"]


def test_search_falls_back_to_best_partial_match():
    # No algorithm contains every token, so the strict AND match is empty.
    # Rather than return nothing, fall back to the highest token overlap —
    # the real fix for descriptive queries like "field calculator area".
    qp = QgisProcess.__new__(QgisProcess)
    qp._algorithms = {
        "native:fieldcalculator": {
            "name": "Field calculator", "group": "Vector table",
            "short_description": "Computes a new field", "tags": ["field"],
            "provider": "native",
        },
        "native:buffer": {
            "name": "Buffer", "group": "g", "short_description": "buffer area",
            "tags": ["buffer"], "provider": "native",
        },
    }
    # "field" + "calculator" hit fieldcalculator (2 tokens); "area" hits buffer
    # (1 token). Best overlap wins, so the field calculator ranks alone.
    ids = [h["id"] for h in qp.search("field calculator area")]
    assert ids == ["native:fieldcalculator"]


def test_search_strict_when_full_match_exists():
    # When some algorithm matches ALL tokens, weaker partial matches are dropped
    # (the fallback only kicks in when nothing matches everything).
    qp = QgisProcess.__new__(QgisProcess)
    qp._algorithms = {
        "native:full": {
            "name": "Buffer area", "group": "g", "short_description": "d",
            "tags": [], "provider": "native",
        },
        "native:partial": {
            "name": "Buffer", "group": "g", "short_description": "d",
            "tags": [], "provider": "native",
        },
    }
    ids = [h["id"] for h in qp.search("buffer area")]
    assert ids == ["native:full"]


@requires_qgis
def test_describe_buffer_lists_parameters():
    d = QgisProcess().describe("native:buffer")
    assert d["id"] == "native:buffer"
    assert "DISTANCE" in d["parameters"]
    assert "INPUT" in d["parameters"]


@requires_qgis
def test_run_buffer_creates_output(tmp_path):
    pt = write_point(tmp_path / "p.geojson", 500000, 5600000, "EPSG:25832")
    out = tmp_path / "buf.geojson"
    res = QgisProcess().run(
        "native:buffer",
        {"INPUT": str(pt), "DISTANCE": 50, "OUTPUT": str(out)},
    )
    assert res["results"]["OUTPUT"]
    assert out.exists()
