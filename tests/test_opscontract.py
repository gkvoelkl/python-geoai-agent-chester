"""Test-Level 1: no geo operation may raise — read off the modules, not off a list.

The rule cost three runs before it became a rule (`chester/opscontract.py` has the
dates). The third time is the reason this file exists: the guard was on `geoops`, the
exception came out of `networkops`, and nothing noticed that three of the four
operation modules had no guard at all. A checked-in list of protected functions would
have drifted the same way, so these tests walk the AST and the modules themselves.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from chester import geoops, networkops, opscontract, rasterops, terrainops

#: The pure operation modules — every public function in them is a tool's core.
OPS_MODULES = (geoops, networkops, rasterops, terrainops)
SRC = Path(__file__).resolve().parent.parent / "packages" / "chester-geo-tools" / "chester"

#: Functions that answer a question about the environment rather than doing work on a
#: layer. They take no paths, write nothing, and have nothing to fail at.
_NOT_OPERATIONS = {"grass_available"}


def _public_functions(module_name: str) -> list[ast.FunctionDef]:
    tree = ast.parse((SRC / f"{module_name}.py").read_text())
    return [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
            and n.name not in _NOT_OPERATIONS]


@pytest.mark.parametrize("module", OPS_MODULES, ids=lambda m: m.__name__)
def test_every_operation_carries_the_contract(module):
    """A module added later must not quietly arrive without the guard."""
    short = module.__name__.rsplit(".", 1)[-1]
    unguarded = [
        fn.name for fn in _public_functions(short)
        if not any(getattr(d, "id", getattr(d, "attr", "")) in {"never_raises",
                                                                "_never_raises"}
                   for d in fn.decorator_list)
    ]
    assert not unguarded, (
        f"{short}: {unguarded} can raise into the agent loop. Decorate with "
        "`@never_raises` — an exception is not a bad answer, it is no answer, and the "
        "run dies holding the work already done."
    )


@pytest.mark.parametrize("module", OPS_MODULES, ids=lambda m: m.__name__)
def test_the_modules_are_actually_covered(module):
    """The AST test would pass on an empty module — this one insists there is
    something to guard, so a rename cannot make the check vacuous."""
    short = module.__name__.rsplit(".", 1)[-1]
    assert _public_functions(short), f"{short} has no public operation any more"


def test_the_guard_converts_an_exception_into_a_return():
    @opscontract.never_raises
    def explodes(x: int) -> dict:
        raise ValueError("no good")

    result = explodes(1)
    assert result["ok"] is False
    assert result["error"] == "ValueError: no good"
    assert "explodes" in result["note"] and "trying a variant" in result["note"]


def test_the_guard_leaves_a_working_call_alone():
    @opscontract.never_raises
    def fine() -> dict:
        return {"ok": True, "output": "x.gpkg"}

    assert fine() == {"ok": True, "output": "x.gpkg"}
    assert fine.__name__ == "fine", "the tool schema is built from the signature"


def test_an_isochrone_without_area_is_refused(tmp_path):
    """A hull with no area answers "nothing is reachable" for every later spatial test
    — and would do it while returning ok. Collinear reached nodes are the case: one
    street, no side roads, or a network clipped to a corridor.
    """
    import geopandas as gpd
    from shapely.geometry import LineString

    cache = tmp_path / "geocache"
    cache.mkdir()
    # One straight street, noded every 100 m: reachable, but flat.
    segments = [LineString([(x, 0), (x + 100, 0)]) for x in range(0, 900, 100)]
    gpd.GeoDataFrame({"id": range(len(segments))}, geometry=segments,
                     crs="EPSG:25832").to_file(cache / "street.gpkg")

    res = networkops.service_area("street.gpkg", "iso.gpkg", start_lon=0.0,
                                  start_lat=0.0, minutes=10, start_crs="EPSG:25832",
                                  workspace=str(tmp_path))
    assert res["ok"] is False
    assert "no area between them" in res["error"]
    assert "side roads" in res["error"], "the error has to name the likely cause"
