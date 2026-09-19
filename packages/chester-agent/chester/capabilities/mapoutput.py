"""MapOutputCapability — render results as an interactive HTML map.

The end of a geo workflow is usually "show me". This wraps GeoPandas' folium-based
``explore`` to write a standalone, interactive HTML map of one or more vector
layers, so Chester can hand the user something to look at.
folium/geopandas are imported lazily to keep startup fast.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import BinaryContent, RunContext, ToolReturn
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import maptools
from chester.mapargs import as_list as _as_list
from chester.mapsnapshot import _render_snapshot
from chester.visioncaps import sees_images
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

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

        # `async def` on purpose, and it is load-bearing: pydantic-ai dispatches a
        # *synchronous* tool through `run_in_executor`, which flags the context so a
        # nested `Agent.run_sync()` fails fast against a possible deadlock. The vision
        # fallback below is exactly such a nested run, so a sync `inspect_map` could
        # never look at anything — every `via_vision_model` call died with a UserError,
        # silently, from the day pydantic-ai added that guard. Async keeps the flag
        # unset; the blocking vision turn then goes off the loop via `to_thread`, the
        # same shape `gate.py` already uses for its level-2 check.
        async def inspect_map(  # noqa: PLR0913  # spiegelt bewusst die Signatur von render_map
            layers: list[str] | None = None,
            question: str | None = None,
            column: str | None = None,
            cmap: str = "YlOrRd",
            scheme: str = "NaturalBreaks",
            k: int = 5,
            via_vision_model: bool = False,
            # Tolerant aliases for the names the model reaches for from
            # `render_map` (`layer`↔`layers`, `lines`→another layer,
            # `columns`↔`column`) — accepted and reconciled so a mis-named
            # arg does not crash the run. `field`/`fields` are display-field
            # lists render_map takes; the flat snapshot has no popups, so they
            # are accepted and ignored rather than raising a validation error.
            layer: str | list[str] | None = None,
            lines: str | list[str] | None = None,
            columns: str | list[str] | None = None,
            field: str | list[str] | None = None,
            fields: str | list[str] | None = None,
        ) -> Any:
            """Render a static snapshot of ``layers`` and return it to *look at*.

            A visual correctness check to run before finalising a non-trivial
            result — a second channel alongside `check_crs`/`sanity_check_result`.
            It draws the layers (raster and/or vector, choropleth if ``column``
            fits) as one flat image and, by default, returns that image plus a
            per-layer fact summary (features, geometry, CRS, WGS84 extent, value
            range) so you can judge placement, extent, coverage, and colour
            variation, then redo the offending step if the picture contradicts the
            task. ``question`` focuses the check.

            **If you cannot see the returned image** (you are a text-only model),
            call again with ``via_vision_model=True``: the configured fallback
            vision model (``model.vision_model``) looks at the snapshot and returns
            a written verdict instead of the image.
            """
            # Reconcile the tolerant aliases onto the canonical params, mirroring
            # render_map, so an arg named like render_map's reconciles instead of
            # crashing the run (a repeated mis-call raises UnexpectedModelBehavior).
            layers = _as_list(layers) or []
            layers += [p for p in (_as_list(layer) or []) if p not in layers]
            layers += [p for p in (_as_list(lines) or []) if p not in layers]
            if column is None and columns is not None:
                col = _as_list(columns) or []
                if len(col) == 1 and "," in col[0]:
                    col = [c.strip() for c in col[0].split(",") if c.strip()]
                if col:
                    column = col[0]
            _ = (field, fields)  # display-field lists — no popups here; ignored.

            if not layers:
                return {"ok": False, "error": "no layers given"}
            try:
                png, summary = _render_snapshot(
                    layers,
                    ws,
                    column=column,
                    scheme=scheme,
                    k=k,
                    cmap=cmap,
                    title=question or "result — visual check",
                )
                # Keep the snapshot as a cache artefact (best-effort).
                snap = str(Path(resolve_path("inspect_snapshot.png", ws, write=True)))
                try:
                    Path(snap).write_bytes(png)
                except OSError:
                    snap = None  # type: ignore[assignment]
                    # Absicht: der Schnappschuss ist optional; scheitert das Schreiben,
                    # laeuft der Rest ohne Bild weiter.
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            # A model that takes no image must never be handed one: Ollama rejects
            # the *request* with HTTP 400 before the model can read the "call again
            # with via_vision_model=True" hint below, the exception aborts the event
            # stream, and a run dying there persists no session at all — 634 s of
            # correct work with nothing to read back (`doc/visual-validation.md` §7).
            # So establish it here instead of waiting for the model to notice.
            # `sees_images` returns None whenever it does not know; only a stated
            # "no vision" reroutes, so an unknown provider behaves exactly as before.
            routed = not via_vision_model and sees_images(main_model, base_url) is False
            if routed and not vision_model:
                return {
                    "ok": True,
                    "layers": summary,
                    "snapshot": snap,
                    "note": f"'{main_model}' takes no image input and no fallback is "
                    "configured — nobody looked at this snapshot. Judge placement and "
                    "extent from the per-layer facts above, say that the visual check "
                    "did not happen, and set model.vision_model to enable it.",
                }
            if via_vision_model or routed:
                # Main model can't see → the configured vision model looks instead.
                if not vision_model:
                    return {
                        "ok": False,
                        "vision_model": None,
                        "layers": summary,
                        "note": "no fallback vision model configured — set "
                        "model.vision_model in .chester/chester.json "
                        "(e.g. 'ollama/llava:latest').",
                    }
                try:
                    review = await asyncio.to_thread(
                        _ask_vision_model, vision_model, base_url, png, question, summary
                    )
                except Exception as exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "layers": summary,
                        "error": f"vision model '{vision_model}' failed: "
                        f"{type(exc).__name__}: {exc}",
                    }
                result = {
                    "ok": True,
                    "reviewed_by": vision_model,
                    "review": review,
                    "layers": summary,
                    "snapshot": snap,
                }
                if routed:
                    # Say who actually looked: the verdict is a second model's
                    # reading of the picture, not the caller's own.
                    result["note"] = (
                        f"'{main_model}' takes no image input, so the snapshot went "
                        f"to '{vision_model}' automatically — the review above is "
                        f"its verdict, not yours."
                    )
                return result

            return ToolReturn(
                return_value={
                    "ok": True,
                    "layers": summary,
                    "snapshot": snap,
                    "instructions": "Look at the attached snapshot: is the data "
                    "placed correctly, the extent plausible, coverage complete, "
                    "the colour varying? If it contradicts the task, redo the "
                    "offending step. If you CANNOT see the image, call inspect_map "
                    "again with via_vision_model=true.",
                },
                content=[
                    "Rendered snapshot for visual validation:",
                    BinaryContent(data=png, media_type="image/png"),
                ],
            )

        return FunctionToolset(tools=[*maptools.build_tools(ws), inspect_map])
