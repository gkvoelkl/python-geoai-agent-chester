"""`inspect_map` — the visual check, shared by every Chester agent.

Moved out of `chester.capabilities.mapoutput` (2026-09-19, KP.5 T2) so chester-team
can hand it to its ressorts. It lives in chester-runtime because it returns a
pydantic-ai `ToolReturn` with the image and, for a text-only model, runs a nested
vision-model turn (`chester.runtime.vision`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic_ai import BinaryContent, ToolReturn

from chester.mapargs import as_list as _as_list
from chester.mapsnapshot import _render_snapshot
from chester.runtime.vision import _ask_vision_model
from chester.visioncaps import sees_images
from chester.workspace import resolve_path


def build_inspect_map(
    ws: str, *, vision_model: str = "", base_url: str = "", main_model: str = ""
) -> Callable[..., Any]:
    """The `inspect_map` tool, bound to workspace and the configured vision fallback."""
    # `async def` on purpose, and it is load-bearing: pydantic-ai dispatches a
    # *synchronous* tool through `run_in_executor`, which flags the context so a
    # nested `Agent.run_sync()` fails fast against a possible deadlock. The vision
    # fallback below is exactly such a nested run, so a sync `inspect_map` could
    # never look at anything — every `via_vision_model` call died with a UserError,
    # silently, from the day pydantic-ai added that guard. Async keeps the flag
    # unset; the blocking vision turn then goes off the loop via `to_thread`, the
    # same shape `gate.py` already uses for its level-2 check.
    async def inspect_map(  # noqa: PLR0913  # mirrors render_map's signature on purpose
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
                # On purpose: the snapshot is optional; if writing fails, the
                # rest continues without a picture.
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

    return inspect_map
