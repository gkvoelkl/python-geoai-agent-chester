"""The vision-model call behind the visual checks — shared by agent and gate.

Two callers need the same thing: `inspect_map` (chester-agent) when the main model
cannot see images, and the gate's level-2 visual check (`chester.runtime.gatehook`).
It used to live in `chester.capabilities.mapoutput`, and the gate reached into the
capability layer for it; with the gate in chester-runtime that import would point
upward, so the call moved here (2026-09-19). What goes to the reviewer beside the
image — the legend and the rules for reading it — moved with it: the prompt and the
call are one thing.
"""

from __future__ import annotations

_DEFAULT_REVIEW_PROMPT = (
    "You are validating a GIS result. Describe what you see, then judge plausibility: "
    "is the data placed where it should be (not off-coast / wrong hemisphere ⇒ a CRS "
    "bug), is the extent sensible, do partition layers tile without gaps/overlaps, and "
    "does a choropleth's colour actually vary? Flag anything that looks wrong."
)


_COLOUR_NAMES = {
    "#3388ff": "blue",
    "#e6550d": "orange",
    "#31a354": "green",
    "#756bb1": "purple",
    "#d62728": "red",
    "#17becf": "cyan",
}


def _legend(summary: list[dict] | None) -> str:
    """Name every drawn layer and its colour, for the reviewer's prompt.

    Without it the reviewer guesses what it is looking at, and it guesses badly:
    on 2026-08-26 it read a blue city boundary as the vegetation layer, and on a
    Regensburg snapshot it reported Munich street names. A snapshot is shapes on
    a backdrop — the meaning lives in the caller's head unless it is written down.
    """
    if not summary:
        return ""
    lines = []
    for s in summary:
        colour = s.get("colour") or "a colour from the map's colourmap"
        colour = _COLOUR_NAMES.get(colour, colour)
        if s.get("type") == "raster":
            lines.append(f"- {s['layer']}: raster overlay, {colour}")
            continue
        geom = "/".join(s.get("geometry_types") or []) or "no"
        lines.append(f"- {s['layer']}: {colour}, {s.get('features', 0)} {geom} features")
    return "The image shows exactly these layers, drawn bottom to top:\n" + "\n".join(lines)


_LEGEND_RULES = (
    "Judge ONLY the layers listed above — the image contains no others. If the "
    "question asks about something that is not in that list (a buffer, a boundary, "
    "a layer that was not passed), say so plainly instead of describing it. Do not "
    "name any town, street or region unless you can read that name as a label in "
    "the image itself."
)


def _review_prompt(question, summary=None) -> str:
    """The text that goes to the reviewer beside the image."""
    prompt = question or _DEFAULT_REVIEW_PROMPT
    legend = _legend(summary)
    return f"{legend}\n\n{prompt}\n\n{_LEGEND_RULES}" if legend else prompt


def _ask_vision_model(model_str: str, base_url: str, png: bytes, question, summary=None) -> str:
    """Send the snapshot to a dedicated vision model and return its written verdict.

    The fallback for a text-only main model: build the configured vision model with
    SelmaKit's own dispatch (so any provider works) and run one multimodal turn.
    """
    from pydantic_ai import Agent
    from pydantic_ai import BinaryContent as _BC
    from selmakit.config import ModelConfig, build_model

    model = build_model(
        ModelConfig(
            model=model_str,
            base_url=base_url or "http://localhost:11434/v1",
        )
    )
    prompt = _review_prompt(question, summary)
    result = Agent(model).run_sync([prompt, _BC(data=png, media_type="image/png")])
    return result.output
