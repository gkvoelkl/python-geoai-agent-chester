"""MapOutputCapability — render results as an interactive HTML map.

The end of a geo workflow is usually "show me". This wraps GeoPandas' folium-based
``explore`` to write a standalone, interactive HTML map of one or more vector
layers, so Chester can hand the user something to look at.
folium/geopandas are imported lazily to keep startup fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import maptools
from chester.runtime.mapinspect import build_inspect_map
from chester.workspace import DEFAULT_WORKSPACE

_VISION_INSTRUCTIONS = """

## Visual validation

Before finalising a non-trivial result, call `inspect_map(layers=[...])` to render
a static snapshot and **look at it** — a second check alongside `check_crs` /
`sanity_check_result` that catches what numbers miss. Judge:
- **Placement** — is the data where the place actually is? (off-coast / wrong
  hemisphere ⇒ a CRS or lon/lat-swap bug.)
- **Extent** — does the footprint match the expected area?
- **Coverage** — do partition layers (Voronoi, districts) tile without gaps/overlaps?
- **Choropleth** — does the colour actually vary? (uniform ⇒ a broken join or a
  constant/null field.)
- **Index maps** — does NDWI/NDVI water/vegetation follow real features, not cloud?

If the picture contradicts the task, diagnose and **redo the offending step**
(reproject, re-join, pick the right layer) rather than reporting a wrong result.
Pass `column` for a choropleth snapshot; `question` to focus the check.

**If you cannot actually see the attached image** (you would say "I see no image"),
you are not a vision model — call `inspect_map(..., via_vision_model=True)` and the
configured fallback vision model looks at the snapshot for you and returns a written
verdict you can act on.\
"""


@dataclass
class MapOutputCapability(AbstractCapability[Any]):
    """Render vector layers to an interactive HTML map via folium."""

    workspace: str = DEFAULT_WORKSPACE
    # Fallback vision model + its base_url (from agent_build/config). inspect_map
    # routes the snapshot here when the main model says it cannot see it — or when
    # `main_model` is one we can establish beforehand takes no image at all.
    vision_model: str = ""
    base_url: str = ""
    # The configured `model.model`, carried only so `chester.visioncaps` can ask the
    # server whether attaching an image to it would abort the run.
    main_model: str = ""

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            # `render_map`'s half comes from the wrapper layer, the vision half is
            # this capability's own — `inspect_map` did not move with it.
            return maptools.instructions() + _VISION_INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace
        vision_model = self.vision_model
        base_url = self.base_url
        main_model = self.main_model

        # The visual check lives in chester-runtime (shared with chester-team).
        inspect_map = build_inspect_map(
            ws, vision_model=vision_model, base_url=base_url, main_model=main_model
        )

        return FunctionToolset(tools=[*maptools.build_tools(ws), inspect_map])
