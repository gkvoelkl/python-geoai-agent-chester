"""What the LLM judge is shown: the whole run, or only its result.

``CHESTER_JUDGE=result`` (2026-10-24) hides the tool-call sequence and the call
arguments from the judge. It grades on what the run **produced** — the layers read from
the files (CRS, size, geometry type, extent) and the final answer — and nothing about the
route. Reason: the architectures now differ in *how* they work (one call per step, a
team, one program), and a judge reading the route grades the route. Unset, the judge sees
everything as before, so existing history stays comparable; the run's cell label should
name the mode.
"""

from __future__ import annotations

import os

ENV = "CHESTER_JUDGE"

RESULT_ONLY_NOTE = (
    "# Mode: judge the RESULT only\n"
    "You are not shown how the agent worked — no tools, no arguments. Judge every "
    "criterion by what the result shows: the produced layers below (read from the files) "
    "and the final answer. A criterion that names a method (\"place= or a clip against "
    "the boundary\", \"in a metric CRS\") is met when the result shows that method's "
    "effect — e.g. a metric CRS on the final layer, an extent that stays within the named "
    "area. Do not fail a criterion for a route you cannot see."
)


def result_only() -> bool:
    """Whether this run's judge sees only the result (``CHESTER_JUDGE=result``)."""
    return os.environ.get(ENV, "").strip().lower() == "result"
