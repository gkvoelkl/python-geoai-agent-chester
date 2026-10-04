"""Das Kartenwerkzeug als **rahmenneutrale** Hülle.

Phase KM, Schritt 1. Möglich wurde sie erst durch Schritt 1.5: `render_map` war
469 Zeilen und hätte jede Hülle über die 400-Zeilen-Grenze getrieben; zerlegt in
`mapargs` · `maprender` · `mapguards` ist es eine Abfolge von Aufrufen.

**Der Docstring ist die Werkzeugbeschreibung** — für einen fremden MCP-Client der
einzige Textkanal, der das Modell nachweislich erreicht (gemessen 2026-09-13).

`inspect_map` kam bewusst **nicht** mit: Es baut über SelmaKit ein Sehmodell und
gibt `ToolReturn` mit `BinaryContent` zurück, ist also an den Rahmen gebunden. Es
bleibt in der Capability.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from chester import mapargs, mapguards, maprender
from chester.mapsnapshot import _is_raster
from chester.qgis_env import qgis_disabled
from chester.workspace import resolve_path


def _feature_counts(names: list[str], resolved: list[str]) -> dict[str, int]:
    """How many features each drawn vector layer holds, by the name the caller used.

    The return is the last thing the model reads before it answers. Asked to *show
    all buildings of Regensburg*, the agent drew 30 194 and then wrote "here are all
    buildings" with no number — the count had scrolled out of view (2026-08-23,
    `show-regensburg-buildings`). Read from the file header (`pyogrio.read_info`), so
    it stays fast at 30k features; rasters and unreadable layers are skipped.
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


_INSTRUCTIONS_TEMPLATE = """\
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

**A selection is drawn as a selection.** When the question asks for *the ten
tallest*, *the five nearest*, *the stops inside X*, write exactly those features to
their own layer first (`vector_filter` on the value, or a snippet) and map that layer
— with a boundary underneath for context if useful. Colouring the whole area by the
value, however clearly the top ones stand out, is a different map than the one asked.

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


def instructions() -> str:
    """Der Instruktionsblock für `render_map`, an die vorhandene Umgebung angepasst.

    Ohne QGIS kein Verweis auf QGIS Desktop: Der Prompt darf kein Werkzeug
    versprechen, das im Katalog fehlt (2026-09-07).
    """
    big = (' The return then also carries `recommend_tool: "qgis_show"`: '
           "offer to open the layer in QGIS Desktop with `qgis_show` (after "
           "the user confirms), and prefer that from the start for a layer "
           "you already know is heavy." if not qgis_disabled() else "")
    return _INSTRUCTIONS_TEMPLATE.replace("{_QGIS_BIG}", big)


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """`render_map`, an ``workspace`` gebunden."""
    ws = workspace

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

    return [render_map]
