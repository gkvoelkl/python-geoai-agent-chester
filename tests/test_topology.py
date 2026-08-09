"""V2 — topology checks (doc/validation-concept.md Ebene 1, `check_topology`).

Covers `geofacts.topology_facts` (in-process self-intersection / overlap / duplicate
/ coverage-hole facts) and the `check_topology` tool. Offline, no QGIS.
"""

from __future__ import annotations

from pathlib import Path

from chester.capabilities.validation import GeoValidationCapability
from chester.geofacts import dangle_facts, topology_facts

from _util import tools_of


def _write_wkt(path: Path, wkts: list[str], crs: str = "EPSG:25832") -> Path:
    import geopandas as gpd
    from shapely import wkt

    gpd.GeoDataFrame(
        {"id": list(range(len(wkts)))},
        geometry=[wkt.loads(w) for w in wkts],
        crs=crs,
    ).to_file(path, driver="GPKG")
    return path


# two unit squares overlapping in a 0.5×1 strip
_OVERLAP = [
    "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
    "POLYGON((0.5 0, 1.5 0, 1.5 1, 0.5 1, 0.5 0))",
]
# two squares sharing only an edge (a clean coverage — touches, not overlaps)
_ADJACENT = [
    "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
    "POLYGON((1 0, 2 0, 2 1, 1 1, 1 0))",
]
_BOWTIE = ["POLYGON((0 0, 1 1, 1 0, 0 1, 0 0))"]        # self-intersecting → invalid
_SELF_CROSS_LINE = ["LINESTRING(0 0, 2 2, 0 2, 2 0)"]   # crossing itself → not simple
_DONUT = ["POLYGON((0 0, 4 0, 4 4, 0 4, 0 0),(1 1, 3 1, 3 3, 1 3, 1 1))"]  # 1 hole


# ── topology_facts ───────────────────────────────────────────────────────────


def test_topology_facts_detects_self_overlap(tmp_path):
    t = topology_facts(_write_wkt(tmp_path / "ov.gpkg", _OVERLAP))
    assert t["overlap_checked"] is True
    assert t["self_overlaps"] == 1


def test_topology_facts_clean_coverage_has_no_overlap(tmp_path):
    t = topology_facts(_write_wkt(tmp_path / "adj.gpkg", _ADJACENT))
    assert t["self_overlaps"] == 0
    assert t["invalid"] == 0


def test_topology_facts_invalid_and_not_simple(tmp_path):
    ti = topology_facts(_write_wkt(tmp_path / "bt.gpkg", _BOWTIE))
    assert ti["invalid"] == 1
    tl = topology_facts(_write_wkt(tmp_path / "ln.gpkg", _SELF_CROSS_LINE))
    assert tl["not_simple"] == 1


def test_topology_facts_duplicates(tmp_path):
    t = topology_facts(_write_wkt(tmp_path / "dup.gpkg", _ADJACENT[:1] * 2))
    assert t["duplicate_geometries"] == 1


def test_topology_facts_coverage_hole(tmp_path):
    t = topology_facts(_write_wkt(tmp_path / "donut.gpkg", _DONUT))
    assert t["union_holes"] == 1


def test_topology_facts_skips_scan_over_cap(tmp_path):
    t = topology_facts(_write_wkt(tmp_path / "cap.gpkg", _ADJACENT), max_overlap_features=1)
    assert t["overlap_checked"] is False
    assert t["self_overlaps"] is None


# ── check_topology tool ──────────────────────────────────────────────────────


def _tool(tmp_path):
    return tools_of(GeoValidationCapability(workspace=str(tmp_path)))["check_topology"]


def test_check_topology_flags_overlap(tmp_path):
    r = _tool(tmp_path)(str(_write_wkt(tmp_path / "ov.gpkg", _OVERLAP)))
    assert r["ok"] is False
    assert any("overlapping feature pair" in w for w in r["warnings"])


def test_check_topology_clean_is_ok(tmp_path):
    r = _tool(tmp_path)(str(_write_wkt(tmp_path / "adj.gpkg", _ADJACENT)))
    assert r["ok"] is True
    assert all("overlapping" not in w for w in r["warnings"])


def test_check_topology_skipped_scan_warns(tmp_path):
    r = _tool(tmp_path)(str(_write_wkt(tmp_path / "adj.gpkg", _ADJACENT)), max_features=1)
    assert any("scan skipped" in w for w in r["warnings"])
    assert r["overlap_checked"] is False


# a chain of two segments plus a short spur off the middle node (the spur is a dangle)
_NETWORK = [
    "LINESTRING(0 0, 10 0)",
    "LINESTRING(10 0, 20 0)",
    "LINESTRING(10 0, 10 5)",
]


def test_dangle_facts_counts_free_ends_and_short(tmp_path):
    d = dangle_facts(str(_write_wkt(tmp_path / "net.gpkg", _NETWORK)), max_dangle_length=6.0)
    assert d["free_ends"] == 3          # two termini + the spur end
    assert d["short_dangles"] == 1      # only the 5 m spur is short
    assert d["line_count"] == 3


def test_dangle_facts_none_on_polygons(tmp_path):
    assert dangle_facts(str(_write_wkt(tmp_path / "p.gpkg", _ADJACENT))) is None


def test_check_topology_network_reports_free_ends(tmp_path):
    r = _tool(tmp_path)(str(_write_wkt(tmp_path / "net.gpkg", _NETWORK)), network=True)
    assert r["free_ends"] == 3
    assert any("free line end" in w for w in r["warnings"])
    assert r["ok"] is True  # free ends alone are not a defect without a length filter


def test_check_topology_network_short_dangle_is_defect(tmp_path):
    r = _tool(tmp_path)(
        str(_write_wkt(tmp_path / "net.gpkg", _NETWORK)), network=True, dangle_length=6.0
    )
    assert r["short_dangles"] == 1
    assert r["ok"] is False
    assert any("short dangle" in w for w in r["warnings"])


def test_check_topology_network_on_polygons_notes_no_lines(tmp_path):
    r = _tool(tmp_path)(str(_write_wkt(tmp_path / "p.gpkg", _ADJACENT)), network=True)
    assert any("line networks" in w for w in r["warnings"])


def test_check_topology_rejects_raster(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    rp = tmp_path / "r.tif"
    with rasterio.open(
        rp, "w", driver="GTiff", height=4, width=4, count=1, dtype="float32",
        crs="EPSG:25832", transform=from_origin(0, 4, 1, 1),
    ) as ds:
        ds.write(np.ones((4, 4), dtype="float32"), 1)
    r = _tool(tmp_path)(str(rp))
    assert r["ok"] is False and "vector" in r["error"]
