"""``QGIS --code`` entry point — runs INSIDE QGIS to start Chester's live bridge.

Launched (windowed) by ``chester/qgis_live_client.launch_qgis``. It imports the
bridge module **standalone by directory** (not via the ``chester`` package), so
it never drags Chester's venv dependencies into QGIS's bundled Python. The
launcher passes this file's directory via ``CHESTER_BRIDGE_DIR``.
"""
import builtins
import os
import sys

_dir = os.environ.get("CHESTER_BRIDGE_DIR")
if not _dir:
    try:
        _dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:  # __file__ may be unset under --code
        _dir = os.getcwd()
sys.path.insert(0, _dir)

import qgis_bridge  # noqa: E402  (standalone module import — only qgis + stdlib)
from qgis.utils import iface  # noqa: E402

_bridge = qgis_bridge.LiveBridge(iface)
_bridge.start()
# Keep a global reference so the QObject (and its C++ QTcpServer) isn't GC'd.
builtins._chester_bridge = _bridge
