"""`gate.inspect_result` reads layers, not views: a map PNG or HTML is not a dataset.

Measured 2026-10-24 (chester-code, `buffer-schools-500m`): a program passed the map's
PNG to `validate_result`, the structural check opened it as a layer, reported
"unreadable (DataSourceError), must_fix" — and the run spent minutes "fixing" a correct
result. Test-Level 1.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Point

from chester import gate


def test_a_map_is_not_read_as_a_layer(tmp_path):
    layer = tmp_path / "buffers.gpkg"
    gpd.GeoDataFrame({"n": [1]}, geometry=[Point(721000, 5428000).buffer(500)],
                     crs="EPSG:25832").to_file(layer, driver="GPKG")
    png = tmp_path / "map.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
    html = tmp_path / "map.html"
    html.write_text("<html></html>")

    out = gate.inspect_result([str(layer), str(png), str(html)], workspace=str(tmp_path))
    assert out["must_fix"] is False, out["findings"]
    assert out["checked"] == [str(layer)]


def test_a_missing_map_is_still_a_finding(tmp_path):
    out = gate.inspect_result([str(tmp_path / "gone.png")], workspace=str(tmp_path))
    assert out["must_fix"] is True
    assert out["findings"][0]["check"] == "exists"
