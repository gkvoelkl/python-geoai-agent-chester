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

from chester import mapargs, mapguards, maprender
from chester.mapargs import as_list as _as_list
from chester.mapsnapshot import _is_raster, _render_snapshot
from chester.qgis_env import qgis_disabled
from chester.visioncaps import sees_images
from chester.workspace import DEFAULT_WORKSPACE, resolve_path


def _feature_counts(names: list[str], resolved: list[str]) -> dict[str, int]:
    """How many features each drawn vector layer holds, by the name the caller used.

    The map's return value is the last thing the model reads before it answers, and
    until 2026-08-23 it held only the output path, the layer names and the
    attribution. Asked to *show all buildings of Regensburg*, the agent drew 30 194
    of them and then wrote "here are all buildings in Regensburg" — no number,
    because the count had scrolled three tool calls up the transcript
    (`show-regensburg-buildings`). An answer about a produced layer should be able
    to say how big it is without remembering.

    Read from the file header (`pyogrio.read_info`), not by loading geometry, so
    this stays a few milliseconds even at 30k features. Rasters have no feature
    count and are skipped; an unreadable layer simply contributes no key.
    """
    counts: dict[str, int] = {}
    for name, path in zip(names, resolved, strict=False):
        if _is_raster(name):
            continue
        try:
            from pyogrio import read_info

            counts[name] = int(read_info(path)["features"])
        except Exception:  # noqa: BLE001 - a diagnostic must never break the map
            continue
    return counts


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


_INSTRUCTIONS = """\
## Map output

When the user wants to see a result, call `render_map` with the layer file path(s)
to produce an interactive HTML map. Pass several layers to stack them (e.g. study
area + result). Both **vector** and **raster** layers work: a raster (`.tif`, a
DEM/slope/index) is shown as an image overlay — single-band rasters are colourised
with `cmap` (e.g. a DEM or TRI), multi-band as an RGB composite. You can stack a
raster under vector layers (e.g. a hillshade with roads on top).

For a **choropleth** (a thematic map coloured by a data value — e.g. population
density per municipality), pass `column` = the field to classify. Tune it with
`scheme` (e.g. "NaturalBreaks", "Quantiles", "EqualInterval"), `k` (class count),
and `cmap` (a matplotlib colormap like "YlOrRd", "Blues"). The choropleth styling
applies to whichever layer contains `column`; other layers keep a plain colour, so
you can still stack a boundary or context layer under it.

`column` is a **single** column name (one field to colour by) — NOT a list of
attributes to display. To show names/addresses in the point/feature popups, pass
`fields=["name", "addr:street", ...]` instead. Don't put several comma-joined
names in `column`.

Then copy the `output` string from the tool's return **character for character**
into your reply. The dashboard embeds the map only when that exact path is in the
text and names a real file, so this is what makes the map appear at all.

Copy the shape of this, with your own path and caption:

    [Luftbild der Altstadt](/Users/x/.chester/workspace/geocache/altstadt_map.html)

Never write a *description* of the path where the path goes. `(_the_path_from_the
_tool_call_)` and `(_remote_path_to_map_)` are dead links, and they are what comes
out when this sentence is paraphrased instead of the `output` value being pasted.
If you do not have the string in front of you, look at the tool return again.

If render_map returns **`ok: false` with `embedded: false`**, the layer is **too
large for an inline web map** — **no file exists**. Do NOT quote any path, and do
not describe a map: there is none to describe. Say the layer is too big to show
inline, and offer to narrow it (one district instead of the city, a filtered
subset).{_QGIS_BIG}\
"""

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
            # Ohne QGIS kein Verweis auf QGIS Desktop: Der Prompt darf kein
            # Werkzeug versprechen, das im Katalog fehlt (2026-09-07).
            big = (" The return then also carries `recommend_tool: \"qgis_show\"`: "
                   "offer to open the layer in QGIS Desktop with `qgis_show` (after "
                   "the user confirms), and prefer that from the start for a layer "
                   "you already know is heavy." if not qgis_disabled() else "")
            return _INSTRUCTIONS.replace("{_QGIS_BIG}", big) + _VISION_INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace
        vision_model = self.vision_model
        base_url = self.base_url
        main_model = self.main_model

        def render_map(  # noqa: PLR0913  # eine Kartenfunktion hat viele Optionen
            # (Ebenen, Spalte, Klassen, Basemap, WMS, Titel, Legende ...); sie in ein
            # Optionsobjekt zu buendeln machte den Werkzeugaufruf fuer das Modell schwerer
            output_path: str,
            layers: list[str] | None = None,
            title: str = "",
            basemap: str = "OpenStreetMap",
            basemap_attribution: str = "",
            column: str | None = None,
            scheme: str = "NaturalBreaks",
            k: int = 5,
            cmap: str = "YlOrRd",
            legend: bool = True,
            fields: list[str] | None = None,
            wms_url: str = "",
            wms_layer: str = "",
            wms_format: str = "image/png",
            wms_attribution: str = "",
            # Tolerant aliases for the plural/singular names the model reaches
            # for (`layer`↔`layers`, `columns`↔`column`, `field`↔`fields`) —
            # accepted so a mis-named arg reconciles instead of crashing.
            layer: str | list[str] | None = None,
            columns: str | list[str] | None = None,
            field: str | list[str] | None = None,
        ) -> dict:
            """Render one or more vector or raster layers to an interactive HTML map.

            ``layers`` is a list of file paths, drawn bottom-to-top in order.
            A raster (``.tif`` DEM/slope/index) is shown as an image overlay —
            single-band colourised with ``cmap``, multi-band as an RGB composite —
            and can be stacked under vector layers.
            ``basemap`` is the background tiles — a folium provider name
            ("OpenStreetMap", "CartoDB positron", "CartoDB dark_matter") or an
            XYZ URL template (then pass ``basemap_attribution``). Writes a
            standalone HTML file to ``output_path`` and returns its absolute path
            (report that path verbatim so the dashboard embeds the map inline).
            Layers are reprojected to WGS84 for web display automatically.
            A flat picture of the same map is written beside the HTML and returned
            as ``picture`` — one map in two forms, for readers and tools that cannot
            run an interactive page.

            For a **choropleth**, pass ``column`` (the numeric field to classify);
            the layer holding it is coloured by a classified ramp with a legend.
            ``scheme`` is a classification scheme ("NaturalBreaks", "Quantiles",
            "EqualInterval", …), ``k`` the class count, ``cmap`` a matplotlib
            colormap. Layers without ``column`` keep a plain colour.

            The embedded GeoJSON (geometry + attributes) is ~90% of the HTML, so
            each layer keeps ONLY the columns that are displayed: the geometry,
            the choropleth ``column``, and any attribute names listed in
            ``fields`` (also what tooltips show). Every other column is dropped —
            pass ``fields`` to keep e.g. ["name", "height"] in the popup. This,
            plus canvas rendering, is what lets a very large layer (100k+
            features) stay a viable inline map.

            An OGC **WMS** service can be overlaid live: pass ``wms_url`` + a
            ``wms_layer`` name (from ``wms_capabilities``); the service renders
            its tiles into the map (display only — a WMS is pictures, not
            data). Works on top of ``layers`` or standalone (then the map
            centres on the WMS layer's advertised bbox).
            """
            # Reconcile the tolerant aliases onto the canonical params (phase A).
            layers, fields, column, refusal = mapargs.normalise_args(
                layers, layer, fields, field, column, columns, wms_url, wms_layer
            )
            if refusal is not None:
                return refusal
            # Absolute so the dashboard's os.path.isfile() resolves it regardless
            # of which directory the dashboard process runs from.
            output_path = str(Path(resolve_path(output_path, ws, write=True)).resolve())
            style = maprender.MapStyle(
                column=column,
                fields=fields,
                scheme=scheme,
                k=k,
                cmap=cmap,
                legend=legend,
                basemap=basemap,
                basemap_attribution=basemap_attribution,
            )
            wms = maprender.WmsSpec(
                url=wms_url, layer=wms_layer, fmt=wms_format, attribution=wms_attribution
            )
            try:
                # Guard (cheap, before the heavy read/render): count features first
                # (phase B). A very large layer makes an inline web map that is slow
                # to build and heavy enough to freeze the dashboard.
                refusal = mapguards.refuse_if_too_many_features(layers, ws)
                if refusal is not None:
                    return refusal

                # Phase C: draw the layers. Everything the loop accumulates lives in
                # `build` — see `maprender.MapBuild`.
                build = maprender.draw_layers(layers, ws, style)
                # Phase D: the WMS. Without a local layer it carries the map alone.
                if build.fmap is None and wms:
                    build.found_on_wms(wms, style)
                if build.fmap is None:
                    return {"ok": False, "error": "all given layers were empty"}
                if wms:
                    build.add_wms(wms)

                # Phase E
                refusal = build.refuse_unmatched_column(column)
                if refusal is not None:
                    return refusal

                # Phase F: zoom, layer control, attribution caption.
                attribution = " · ".join(sorted(build.attributions))
                build.fit_to_rasters()  # a raster-founded map still sits at zoom 10
                build.decorate(title, attribution)

                # Phase G: too many vertices — the way out is the picture, not nothing.
                refusal = mapguards.picture_instead_of_map(
                    build.total_vertices, output_path, build.drawn, ws, style, title
                )
                if refusal is not None:
                    return refusal

                # Phase H: write, then the hard backstop on the produced size.
                build.fmap.save(output_path)
                refusal = mapguards.refuse_if_too_large(output_path)
                if refusal is not None:
                    return refusal

                # Pointer to the last rendered map so the `/qgis` command can open
                # the SAME source layers in QGIS Desktop (QGIS can't read the
                # Folium HTML). Best-effort — a failed pointer never breaks the map.
                try:
                    import json as _json

                    (Path(output_path).parent / "last_map.json").write_text(
                        _json.dumps(
                            {"html": output_path, "layers": build.drawn_resolved, "column": column},
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            result = {"ok": True, "output": output_path, "layers": build.drawn}
            counts = _feature_counts(build.drawn, build.drawn_resolved)
            if counts:
                result["features"] = counts
            # `style.k`, not `build.choro_k`: the picture is drawn from the
            # *requested* class count, and on a plain map `choro_k` is None — it
            # only carries a value when a choropleth was actually applied.
            picture = mapguards.picture_beside(
                output_path, build.drawn, ws, style, title
            )
            if picture:
                result["picture"] = picture
            if wms:
                result["wms"] = {"url": wms.url, "layer": wms.layer}
            if build.choro_applied:
                result["choropleth"] = {
                    "column": column,
                    "scheme": scheme,
                    "k": build.choro_k,
                    "cmap": cmap,
                }
            if attribution:
                result["attribution"] = sorted(build.attributions)
            if build.styling:
                result["styling"] = build.styling
                if not build.choro_applied:
                    # The model cannot see the map. Without this it describes the
                    # palette it *asked* for: the run of 2026-08-26 passed
                    # cmap="Greens" and then told the reader about light and dark
                    # green areas on a map whose greenery was blue.
                    shown = ", ".join(
                        f"{name}={style['colour']}" for name, style in build.styling.items()
                    )
                    result["warning"] = (
                        "no `column` was given, so `cmap`/`scheme`/`k` had NO effect — "
                        f"each layer got a fixed colour, in the order listed: {shown}. "
                        "The LAST layer is drawn on top and hides the ones below. "
                        "Describe these colours, not the ones you requested."
                    )
            return result

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

        return FunctionToolset(tools=[render_map, inspect_map])
