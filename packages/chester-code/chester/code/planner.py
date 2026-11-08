"""The requirement step of chester-code: fix area, data, sources and CRS before any code.

After GeoColab (Wu, Gui et al., IJDE 2025): a product-manager role first turns the request
into a requirement document — goal, spatial extent, data source per layer, CRS, method,
output — and only then is code written. Measured there: the procedure alone, without its
knowledge base, already adds 1.8–3.2 % executability. Measured here (2026-10-24,
`mean-elevation-per-district`): without it the coder decided *inside* its programs where
the city districts come from, and after 18 programs had not decided.

One extra model call per run, with **no tools** and a structured output; the chosen
sources pick the handbooks (:mod:`chester.code.handbooks`, after LLM-Find) that go into
the coder's instructions. A failed or empty plan changes nothing — the coder then runs
as before, with every tool.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from chester.code import handbooks

log = logging.getLogger(__name__)

SourceId = Literal["osm", "boundaries", "city_districts", "terrain", "aerial", "satellite",
                   "statistics", "transit", "buildings3d", "none"]


class Layer(BaseModel):
    """One data layer the task needs and where it comes from."""

    layer: str = Field(description="what the layer holds, e.g. 'city districts', 'elevation'")
    source: SourceId = Field(description="the source id from the index; 'none' if no source "
                                         "fits or the layer is computed, not fetched")


class Requirement(BaseModel):
    """The requirement document — written before any program."""

    goal: str = Field(description="what the result answers, in one sentence")
    area: str = Field(description="the named area and the boundary to clip to")
    layers: list[Layer] = Field(description="every input layer, each with its source")
    crs: str = Field(description="the CRS of the computation, e.g. EPSG:25832 in Germany")
    method: str = Field(description="the computation, as short steps")
    output: str = Field(description="the files and numbers the answer delivers")

    def sources(self) -> list[str]:
        return [x.source for x in self.layers if x.source != "none"]

    def render(self) -> str:
        layers = "\n".join(f"  - {x.layer} ← `{x.source}`" for x in self.layers)
        return (f"- Goal: {self.goal}\n- Area: {self.area}\n- Layers:\n{layers}\n"
                f"- CRS: {self.crs}\n- Method: {self.method}\n- Output: {self.output}")


PLANNER_INSTRUCTIONS = f"""\
You write the requirement document for a geo task, before anyone writes code. Do not
solve the task. Fill every field from the request; complete what it leaves open with the
obvious choice for a GIS expert:

- Area: the named place and its official boundary (a city is clipped to its city
  boundary, never to a bounding box).
- Layers: every input the computation needs, each with exactly one source from the index
  below. Sub-municipal units (Stadtbezirke, Stadtteile) are `city_districts`, not
  `boundaries`. A layer that is computed from others has source `none`.
- CRS: a metric CRS for distances and areas — EPSG:25832 in Germany, EPSG:2056 in
  Switzerland, EPSG:31287 in Austria.

Source index:
{handbooks.source_index()}
"""


async def plan(model, prompt: str) -> Requirement | None:
    """Run the requirement step on ``model``; None when it fails — the coder runs unplanned."""
    from pydantic_ai import Agent

    try:
        result = await Agent(model, output_type=Requirement,
                             instructions=PLANNER_INSTRUCTIONS).run(prompt)
    except Exception as exc:  # noqa: BLE001 — a plan is a help, never a precondition
        log.warning("requirement step failed: %s: %s", type(exc).__name__, exc)
        return None
    log.info("requirement:\n%s", result.output.render())
    return result.output


def prompt_text(prompt) -> str:
    """The user's text out of a run prompt (a string or a list of user content)."""
    if isinstance(prompt, str):
        return prompt
    return "\n".join(p for p in (prompt or ()) if isinstance(p, str))


def instructions(req: Requirement) -> str:
    """The plan block for the coder: the requirement and the handbooks of its sources."""
    books = handbooks.render(req.sources())
    return (
        "## The requirement for this task (fixed before coding — follow it)\n\n"
        f"{req.render()}\n\n"
        "Write the program for exactly this requirement: fetch each layer from its "
        "source as its handbook shows, then compute and deliver the output. Do not search "
        "for other sources unless a fetch fails."
        + (f"\n\n## Handbooks of the chosen sources\n\n{books}" if books else "")
    )
