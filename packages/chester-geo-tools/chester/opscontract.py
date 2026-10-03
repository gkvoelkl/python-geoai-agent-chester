"""The one promise every geo operation makes: it reports failure, it never raises.

The rule is older than this module and was learned three times, each time expensively,
each time in a different place:

* **2026-09-07** — a misspelt layer name made `gpd.read_file` raise `DataSourceError`.
  The exception left the tool and ended a 930-second run. A catch for that one
  exception was added to :mod:`chester.geoops`.
* **2026-09-21, evening** — `gpd.overlay` raised `NotImplementedError` on a mixed OSM
  layer and took a ressort run with it. The catch from September was widened to every
  exception — still only in :mod:`chester.geoops`.
* **2026-09-21, an hour later** — `shapely.concave_hull` raised inside
  :func:`chester.networkops.service_area`, which lives in a *different* module, and
  ended the next run. The guard was on the module where the damage had happened,
  not on the contract.

Hence this module, which belongs to no operation in particular. A tool return is the
only channel an agent has: an exception is not a bad answer, it is **no** answer, and
the run dies holding whatever work was already done. Every public operation in
`geoops`, `networkops`, `rasterops` and `terrainops` is decorated, and
`tests/test_opscontract.py` checks that none is forgotten — a list of decorated
functions would drift, so the test reads the modules.

What this does **not** do is make a failure look like a success. The return is
``ok: false`` with the exception type and text, plus a note that repeating the same
call will fail the same way — the model needs to change something, not try again.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any


def never_raises(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Turn any escaping exception into the operation's own failure return."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - the contract: nothing leaves the tool
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "note": f"`{fn.__name__}` failed on this input; nothing was written. "
                        "Read the error before trying a variant — the same call again "
                        "will fail the same way.",
            }

    return wrapper
