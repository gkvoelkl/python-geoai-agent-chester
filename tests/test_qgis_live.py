"""Tests for the live-QGIS capability (GeoLiveCapability), incl. the point-cloud path.

Tool wiring is offline; the actual COPC load path is verified headless via QGIS's
bundled Python (`requires_qgis`) — the interactive 3D window still needs a manual check,
like `qgis_show_3d`.
"""

from __future__ import annotations

import pytest
from _util import requires_qgis, tools_of

from chester.capabilities import GeoLiveCapability


def test_live_tools_wired():
    tools = tools_of(GeoLiveCapability(workspace="/tmp/ws"))
    assert {"qgis_show", "qgis_show_3d", "qgis_show_pointcloud",
            "qgis_screenshot", "qgis_save_project"} <= set(tools)


def test_qgis_show_pointcloud_no_layers():
    tools = tools_of(GeoLiveCapability(workspace="/tmp/ws"))
    r = tools["qgis_show_pointcloud"]([])
    assert r["ok"] is False and "no layers" in r["error"]


def test_pointcloud_to_copc_wired_and_guards_missing_file():
    from chester.capabilities import DataDiscoveryCapability

    tools = tools_of(DataDiscoveryCapability(workspace="/tmp/ws"))
    assert "pointcloud_to_copc" in tools
    r = tools["pointcloud_to_copc"]("/no/such/cloud.laz")
    assert r["ok"] is False and "no such point cloud" in r["error"]


@requires_qgis
@pytest.mark.network
def test_pointcloud_to_copc_converts_laz(tmp_path):
    """End-to-end: a LAZ → COPC via pdal:createcopc, and the result loads."""
    import urllib.request

    from chester import qgis_python
    from chester.capabilities import DataDiscoveryCapability

    laz = tmp_path / "sample.laz"
    urllib.request.urlretrieve(
        "https://s3.amazonaws.com/hobu-lidar/autzen-classified.copc.laz", str(laz))
    tools = tools_of(DataDiscoveryCapability(workspace=str(tmp_path)))
    r = tools["pointcloud_to_copc"](str(laz))
    assert r["ok"], r
    assert r["output"].endswith(".copc.laz")
    chk = qgis_python.run_pyqgis(
        "from qgis.core import QgsPointCloudLayer\n"
        f"lyr=QgsPointCloudLayer(r'{r['output']}','o','copc')\n"
        "result={'valid':lyr.isValid()}", timeout=60)
    assert chk["ok"] and chk["result"]["valid"] is True


@requires_qgis
@pytest.mark.network
def test_copc_loads_as_valid_pointcloud_layer():
    """The core of qgis_show_pointcloud: QGIS's `copc` provider loads a COPC (here a
    remote HTTP range-read URL) as a valid QgsPointCloudLayer."""
    from chester import qgis_python

    code = """
from qgis.core import QgsPointCloudLayer
url = 'https://s3.amazonaws.com/hobu-lidar/autzen-classified.copc.laz'
lyr = QgsPointCloudLayer(url, 'autzen', 'copc')
result = {'valid': lyr.isValid(), 'points': lyr.pointCount() if lyr.isValid() else 0}
"""
    r = qgis_python.run_pyqgis(code, timeout=120)
    assert r["ok"], r
    assert r["result"]["valid"] is True and r["result"]["points"] > 0
