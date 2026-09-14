"""MapOutputCapability — render results as an interactive HTML map.

The end of a geo workflow is usually "show me". This wraps GeoPandas' folium-based
``explore`` to write a standalone, interactive HTML map of one or more vector
layers, so Chester can hand the user something to look at.
folium/geopandas are imported lazily to keep startup fast.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import BinaryContent, RunContext, ToolReturn
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import provenance
from chester.mapsnapshot import (
    _COLORS,
    _is_raster,
    _raster_rgba_and_bounds,
    _render_snapshot,
)
from chester.qgis_env import qgis_disabled
from chester.visioncaps import sees_images
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

# A few distinct colours for stacking layers.

# Guardrails against an inline web map that freezes the dashboard: above these a
# layer is too heavy to embed in the chat (a 490 MB HTML did exactly that) and is
# steered to QGIS Desktop (`qgis_show`) instead.
_MAX_INLINE_FEATURES = 50_000  # cheap pre-check, before any heavy read/render
_MAX_INLINE_MB = 45  # hard backstop on the produced HTML size

# The guard that actually predicts a white page. Feature count and byte size both
# missed the case that motivated this: 6.888 contour lines — far under the 50.000
# feature limit — inlined as 38,5 MB of coordinates on a **single 40 MB line**, and
# the finished HTML came to 42,5 MB, just under the 45 MB backstop. Both guards said
# "fine"; the browser showed a white screen (2026-09-02,
# `pluvial-flow-accumulation-tegernheim`). What costs the browser is neither the
# number of features nor the bytes on disk but the number of vertices it must parse
# and turn into paths, so that is what is counted. The observed case measures
# 936.687 vertices; an ordinary city layer sits at a few tens of thousands. 500k
# separates the two by a wide margin in both directions.
_MAX_INLINE_VERTICES = 500_000

# Raster layers render as a Folium ImageOverlay (reprojected to WGS84) rather
# than through the vector reader. These extensions route a layer to that path.
# Cap the overlay image's longest side — a full-res raster PNG would bloat the HTML.


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








# OpenStreetMap's tile policy requires a User-Agent that identifies the application.
# contextily's default is `contextily-<random uuid>`, which OSM answers with HTTP 403
# "Access blocked" tiles — and `add_basemap` composites those error images into the
# plot instead of raising, so the failure is invisible until you look at the picture.
# Measured 2026-08-16: every snapshot outside the aerial coverage was a wall of 403s.


# Below this greyscale standard deviation an image carries no picture. Measured:
# a WMS answer outside its coverage is pure white (std 0.00, one colour value),
# real imagery over Regensburg is std 34 across 228 values — no tuning needed.






# A layer with one feature has a zero-width bounding box, so the plot has no extent
# and the backdrop is stretched over a sliver — measured on a single point:
# extent [12.1, 49.0, 12.1, 49.0]. ~0.02° is roughly 2 km, enough context to see
# where a point sits.




def _write_picture_beside(
    html_path: str, layers: list[str], ws: str,
    column: str | None, scheme: str | None, k: int | None, cmap: str, title: str,
) -> str | None:
    """A flat picture of the same map, written beside the HTML. ``None`` on failure.

    The interactive map and the picture are two forms of one artefact, not two
    answers. The picture is useful on its own: a vision model cannot read HTML (the
    visual check already renders one for exactly that reason), a report can embed
    it, and it survives without a browser, a network or the CDN libraries the HTML
    builds itself from.

    Written unconditionally and never mentioned to the model. **Which form a reader
    receives is not decided here** — a channel that can show HTML shows it, one that
    cannot looks beside it. Making this conditional on the destination would put
    channel knowledge into a tool, and the same agent serves the web chat, a chat
    bot and the CLI.

    Never fatal: the interactive map is the primary artefact and a failed picture
    must not cost the caller their result.
    """
    try:
        png_bytes, _summary = _render_snapshot(layers, ws, column, scheme, k, cmap, title)
        target = str(Path(html_path).with_suffix(".png"))
        Path(target).write_bytes(png_bytes)
        return target
    except Exception:  # noqa: BLE001 - a second form is never worth a failed map
        return None






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


def _as_list(value) -> list | None:
    """Coerce a render_map alias value to a list (or None).

    Accepts a real list, a scalar (→ one-item list), or a JSON-array *string*
    like ``'["a.gpkg"]'`` the model sometimes passes — so an aliased ``layer=…``
    reconciles to ``layers`` cleanly.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return value
    s = str(value).strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
    return [value] if s else None


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
            # Reconcile the tolerant aliases onto the canonical params.
            if not layers:
                layers = _as_list(layer)
            if fields is None and field is not None:
                fields = _as_list(field)
            if column is None and columns is not None:
                col = _as_list(columns) or []
                # The model reaches for `columns` (plural) as a *display-field
                # list* and comma-joins it into one string ("shop,name") — but a
                # choropleth is a single numeric column. Split the string, and if
                # several names result they were meant as popup `fields`, not one
                # choropleth: route them there instead of erroring on a bogus
                # "shop,name" column name.
                if len(col) == 1 and "," in col[0]:
                    col = [c.strip() for c in col[0].split(",") if c.strip()]
                if len(col) > 1:
                    merged = list(fields) if fields else []
                    merged += [c for c in col if c not in merged]
                    fields = merged
                elif col:
                    column = col[0]

            if not layers and not (wms_url and wms_layer):
                return {"ok": False, "error": "no layers given"}
            layers = layers or []
            # Absolute so the dashboard's os.path.isfile() resolves it regardless
            # of which directory the dashboard process runs from.
            output_path = str(Path(resolve_path(output_path, ws, write=True)).resolve())
            try:
                import geopandas as gpd

                # Guard (cheap, before the heavy read/render): count features first.
                # A very large layer makes an inline web map that is slow to build
                # and heavy enough to freeze the dashboard — steer it to QGIS instead.
                total_features = 0
                for path in layers:
                    try:
                        import pyogrio

                        total_features += int(pyogrio.read_info(resolve_path(path, ws))["features"])
                    except Exception:  # noqa: BLE001 — unknown count must not block
                        pass
                if total_features > _MAX_INLINE_FEATURES:
                    return {
                        "ok": False,  # nothing rendered — see the size guard below
                        "embedded": False,
                        "features": total_features,
                        "reason": (
                            f"{total_features} features exceed the inline-map limit "
                            f"({_MAX_INLINE_FEATURES}); an inline web map would be too "
                            "heavy for the dashboard. NO file was written; there is no "
                            "map to link to."
                        ),
                        "recommend_tool": "qgis_show",
                    }

                fmap = None
                total_vertices = 0
                drawn = []
                drawn_resolved: list[str] = []  # absolute paths, for /qgis
                styling: dict[str, dict] = {}  # what was really drawn, per layer
                choro_applied = False
                raster_drawn = False
                # Ein Raster als Basisebene setzt nur den Mittelpunkt; ohne
                # `zoom_start` nimmt folium seine Vorgabe 10. Für ein Ausschnitts-
                # raster ist das eine leere Karte — siehe `fit_bounds` unten.
                raster_bounds: list[list[list[float]]] = []
                fmap_from_raster = False
                attributions: set[str] = set()
                available_columns: set[str] = set()  # union, for a helpful error
                for i, path in enumerate(layers):
                    resolved = resolve_path(path, ws)

                    # Raster layer → a colourised ImageOverlay (WGS84), not the
                    # vector reader (which can't open a GeoTIFF).
                    if _is_raster(path):
                        import folium

                        rgba, bounds, _scale = _raster_rgba_and_bounds(resolved, cmap)
                        raster_bounds.append(bounds)
                        if fmap is None:  # raster is the base — make the map ourselves
                            center = [
                                (bounds[0][0] + bounds[1][0]) / 2,
                                (bounds[0][1] + bounds[1][1]) / 2,
                            ]
                            fmap = folium.Map(
                                location=center,
                                tiles=basemap,
                                attr=basemap_attribution or None,
                            )
                            fmap_from_raster = True
                        folium.raster_layers.ImageOverlay(
                            image=rgba,
                            bounds=bounds,
                            opacity=0.75,
                            name=path.split("/")[-1],
                        ).add_to(fmap)
                        raster_drawn = True
                        drawn.append(path)
                        drawn_resolved.append(str(Path(resolved).resolve()))
                        meta = provenance.read_meta(resolved)
                        if meta and meta.get("licence"):
                            attributions.add(meta["licence"])
                        continue

                    gdf = gpd.read_file(resolved)
                    if gdf.empty:
                        continue
                    # Stützpunkte zählen, solange die Geometrie ohnehin im Speicher
                    # liegt: `pyogrio.read_info` kennt die Objektzahl, nicht die
                    # Stützpunkte — und die entscheidet über die Renderlast.
                    try:
                        import shapely

                        total_vertices += int(
                            shapely.get_num_coordinates(gdf.geometry.values).sum()
                        )
                    except Exception:  # noqa: BLE001 — unbekannt darf nicht blockieren
                        pass
                    available_columns.update(c for c in gdf.columns if c != gdf.geometry.name)
                    if gdf.crs and gdf.crs.to_epsg() != 4326:
                        gdf = gdf.to_crs(4326)
                    # Keep only the columns that will be drawn/shown: geometry,
                    # the choropleth column, and any requested `fields`. The
                    # embedded GeoJSON dominates the HTML size, so dropping unused
                    # attribute columns is the single biggest size win.
                    keep = set(fields or [])
                    if column and column in gdf.columns:
                        keep.add(column)
                    gdf = gdf[[c for c in gdf.columns if c == gdf.geometry.name or c in keep]]
                    explore_kwargs: dict = {
                        "m": fmap,
                        "name": path.split("/")[-1],
                    }
                    # Choropleth for the layer that carries the requested column;
                    # other layers fall back to a plain stacking colour.
                    if column and column in gdf.columns:
                        # Clamp class count to the distinct values present — a
                        # scheme like NaturalBreaks fails when k exceeds them.
                        use_k = max(1, min(k, int(gdf[column].nunique(dropna=True))))
                        explore_kwargs.update(
                            column=column,
                            scheme=scheme,
                            k=use_k,
                            cmap=cmap,
                            legend=legend,
                            style_kwds={"fillOpacity": 0.7},
                        )
                        choro_applied = True
                        choro_k = use_k
                    else:
                        explore_kwargs["color"] = _COLORS[i % len(_COLORS)]
                        explore_kwargs["style_kwds"] = {"fillOpacity": 0.3}
                    n_points = int(gdf.geometry.geom_type.isin(("Point", "MultiPoint")).sum())
                    if n_points:
                        # Folium draws a point as a 10 px CircleMarker. An OSM road
                        # layer carries thousands of nodes (crossings, signals), and
                        # at that radius they cover every polygon below — the
                        # road-impact map of 2026-08-26 answered with orange areas
                        # that no one could see under 4585 green dots.
                        explore_kwargs["marker_kwds"] = {"radius": 3 if n_points > 500 else 6}
                    styling[path] = {
                        "colour": explore_kwargs.get("color", f"choropleth({cmap})"),
                        "points_as_markers": n_points,
                    }
                    if fmap is None:  # the first layer sets the base tiles
                        explore_kwargs["tiles"] = basemap
                        # Canvas rendering draws all features onto one <canvas>
                        # instead of one DOM/SVG node each — the difference
                        # between "loads" and "usable" at 100k+ features.
                        explore_kwargs["prefer_canvas"] = True
                        if basemap_attribution:
                            explore_kwargs["attr"] = basemap_attribution
                    fmap = gdf.explore(**explore_kwargs)
                    drawn.append(path)
                    drawn_resolved.append(str(Path(resolved).resolve()))
                    # Attribution from the layer's provenance sidecar (OSM/basemaps
                    # are licensed and must be credited on the rendered map).
                    meta = provenance.read_meta(resolved)
                    if meta and meta.get("licence"):
                        attributions.add(meta["licence"])

                # WMS-only map: no local layers set the extent, so centre on the
                # WMS layer's advertised WGS84 bbox (one capabilities request).
                if fmap is None and wms_url and wms_layer:
                    import folium

                    center, zoom = [51.0, 10.0], 6  # fallback: DE overview
                    try:
                        from owslib.wms import WebMapService

                        for ver in ("1.3.0", "1.1.1"):
                            try:
                                wms = WebMapService(url=wms_url, version=ver)
                                bb = getattr(wms.contents.get(wms_layer), "boundingBoxWGS84", None)
                                if bb:
                                    center = [(bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2]
                                    zoom = 10
                                break
                            except Exception:  # noqa: BLE001 - try older version
                                continue
                    except Exception:  # noqa: BLE001 - capabilities are best-effort
                        pass
                    fmap = folium.Map(
                        location=center,
                        zoom_start=zoom,
                        tiles=basemap,
                        attr=basemap_attribution or None,
                    )

                if fmap is None:
                    return {"ok": False, "error": "all given layers were empty"}

                wms_added = False
                if wms_url and wms_layer:
                    import folium

                    folium.WmsTileLayer(
                        url=wms_url,
                        layers=wms_layer,
                        fmt=wms_format,
                        transparent=True,
                        overlay=True,
                        name=f"WMS: {wms_layer}",
                        attr=wms_attribution or wms_url,
                    ).add_to(fmap)
                    wms_added = True
                    if wms_attribution:
                        attributions.add(wms_attribution)
                # A column that matched no vector layer is only an error when no
                # raster was drawn — `column` simply doesn't apply to a raster.
                if column and not choro_applied and not raster_drawn:
                    cols = ", ".join(sorted(available_columns)) or "(none)"
                    return {
                        "ok": False,
                        "error": (
                            f"column '{column}' not found in any layer. "
                            f"`column` is a SINGLE column to colour a choropleth by "
                            f"(usually numeric); it is not a list of popup fields — "
                            f"pass those as `fields=[...]`. Available columns: {cols}"
                        ),
                    }

                attribution = " · ".join(sorted(attributions))
                # ── Auf die Daten zoomen, wenn ein Raster die Karte aufgespannt hat ──
                # Gemessen 2026-09-07 (`swiss-terrain-slope-grindelwald`): Das
                # Hangneigungsraster deckte 1,1 x 0,8 km ab, die Karte öffnete auf
                # foliums Vorgabezoom 10 — rund 50 km Blickfeld, das Raster ein paar
                # Pixel groß. Die Judges sahen die richtige Rechnung und gaben
                # „passt"; auf der Karte war nichts zu sehen. Vektorkarten trifft es
                # nicht, `gdf.explore()` zoomt selbst auf seine Daten; genau deshalb
                # ist der Fehler nur beim reinen Rasterfall so lange durchgerutscht.
                if fmap_from_raster and raster_bounds:
                    south = min(b[0][0] for b in raster_bounds)
                    west = min(b[0][1] for b in raster_bounds)
                    north = max(b[1][0] for b in raster_bounds)
                    east = max(b[1][1] for b in raster_bounds)
                    with contextlib.suppress(Exception):  # kosmetisch, nie fatal
                        fmap.fit_bounds([[south, west], [north, east]])

                try:
                    import folium

                    folium.LayerControl().add_to(fmap)
                    if title:
                        folium.map.Marker(
                            [0, 0],
                            icon=folium.DivIcon(
                                html=f'<div style="font-weight:bold">{title}</div>'
                            ),
                        )
                    if attribution:
                        # branca liefert keine Stubs fuer get_root().html
                        fmap.get_root().html.add_child(  # type: ignore[attr-defined]
                            folium.Element(
                                '<div style="position:fixed;bottom:8px;left:8px;'
                                "z-index:9999;background:rgba(255,255,255,0.8);"
                                "padding:2px 6px;font-size:11px;border-radius:3px;"
                                'font-family:sans-serif">'
                                f"{attribution}</div>"
                            )
                        )
                except Exception:  # noqa: BLE001 - layer control/caption are cosmetic
                    pass

                # ── Stützpunkt-Wächter: der Ausweg ist das Bild, nicht das Nichts ──
                # Der Vorgänger dieses Wächters warf die HTML weg und meldete
                # `ok: false` — richtig, solange nichts Brauchbares entstanden war.
                # Hier ist es anders: Dieselbe Karte gibt es als PNG, und ein PNG
                # ist genau das, was ein Leser bei zu vielen Linien ohnehin braucht.
                # Es als Fehlschlag zu melden hiesse, ein Ergebnis zu verschweigen,
                # das auf der Platte liegt — die gespiegelte Form desselben Fehlers.
                if total_vertices > _MAX_INLINE_VERTICES:
                    picture = _write_picture_beside(
                        output_path, drawn, ws, column, scheme, k, cmap, title
                    )
                    Path(output_path).unlink(missing_ok=True)
                    if not picture:
                        return {
                            "ok": False,
                            "embedded": False,
                            "vertices": total_vertices,
                            "reason": (
                                f"{total_vertices:,} vertices exceed the inline-map "
                                f"limit ({_MAX_INLINE_VERTICES:,}) and the fallback "
                                "picture could not be rendered either. NO file was "
                                "written; there is no map to link to."
                            ),
                            "recommend_tool": "qgis_show",
                        }
                    return {
                        "ok": True,
                        "embedded": False,
                        "output": picture,
                        "picture": picture,
                        "layers": drawn,
                        "vertices": total_vertices,
                        "reason": (
                            f"{total_vertices:,} vertices exceed the inline-map limit "
                            f"({_MAX_INLINE_VERTICES:,}) — an interactive web map with "
                            "that many would open as a blank page. A STATIC PICTURE was "
                            "written instead; report its path, it is a real result. For "
                            "an interactive map, reduce the geometry first (a coarser "
                            "contour interval, a simplify step, fewer layers) or open the "
                            "source layers in QGIS with qgis_show."
                        ),
                        "recommend_tool": "qgis_show",
                    }

                fmap.save(output_path)
                # Hard backstop: if the produced HTML is too big to embed safely,
                # delete it and steer to QGIS rather than freeze the dashboard.
                size_mb = Path(output_path).stat().st_size / (1024 * 1024)
                if size_mb > _MAX_INLINE_MB:
                    Path(output_path).unlink(missing_ok=True)
                    # `ok: False`: the HTML was written and then deleted, so nothing
                    # exists at output_path. `ok: True` for a file that is not there
                    # invites the answer to describe a map nobody can open — one run
                    # did exactly that, complete with an excuse for why it might not
                    # appear. No path back means no success to report.
                    return {
                        "ok": False,
                        "embedded": False,
                        "size_mb": round(size_mb, 1),
                        "reason": (
                            f"the rendered map is {size_mb:.0f} MB (limit "
                            f"{_MAX_INLINE_MB} MB) — too heavy to embed in the chat. "
                            "NO file was written; there is no map to link to."
                        ),
                        "recommend_tool": "qgis_show",
                    }
                # Pointer to the last rendered map so the `/qgis` command can open
                # the SAME source layers in QGIS Desktop (QGIS can't read the
                # Folium HTML). Best-effort — a failed pointer never breaks the map.
                try:
                    import json as _json

                    (Path(output_path).parent / "last_map.json").write_text(
                        _json.dumps(
                            {"html": output_path, "layers": drawn_resolved, "column": column},
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

            result = {"ok": True, "output": output_path, "layers": drawn}
            counts = _feature_counts(drawn, drawn_resolved)
            if counts:
                result["features"] = counts
            # `k`, not `choro_k`: the latter is only bound inside the choropleth
            # branch, so a plain map would raise UnboundLocalError here.
            picture = _write_picture_beside(
                output_path, drawn, ws, column, scheme, k, cmap, title
            )
            if picture:
                result["picture"] = picture
            if wms_added:
                result["wms"] = {"url": wms_url, "layer": wms_layer}
            if choro_applied:
                result["choropleth"] = {
                    "column": column,
                    "scheme": scheme,
                    "k": choro_k,
                    "cmap": cmap,
                }
            if attribution:
                result["attribution"] = sorted(attributions)
            if styling:
                result["styling"] = styling
                if not choro_applied:
                    # The model cannot see the map. Without this it describes the
                    # palette it *asked* for: the run of 2026-08-26 passed
                    # cmap="Greens" and then told the reader about light and dark
                    # green areas on a map whose greenery was blue.
                    shown = ", ".join(
                        f"{name}={style['colour']}" for name, style in styling.items()
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
