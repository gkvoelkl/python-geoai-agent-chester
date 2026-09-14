"""Die Karte im Bau — Zustand und Ebenenschleife von `render_map`. Ein reiner Kern.

Phase KM, Schritt 1.5. `render_map` war 469 Zeilen lang, davon ein einziger
`try:`-Block über 340; als Ganzes passte es in kein Hüllenmodul. Zerlegt in vier
Begriffe statt einen Block:

| | |
|---|---|
| Was das Modell gemeint hat (Aliasnamen) | `chester/mapargs.py` |
| **Wie die Karte entsteht** (Zustand, Ebenen, WMS) | *hier* |
| Wann sie *nicht* ausgeliefert wird (die drei Wächter) | `chester/mapguards.py` |
| Das Standbild daneben | `chester/mapsnapshot.py` |

Kein `pydantic_ai`, kein `selmakit` — beides würde die Reinheitsprüfung auslösen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chester import provenance
from chester.mapsnapshot import _COLORS, _is_raster, _raster_rgba_and_bounds
from chester.workspace import resolve_path


@dataclass
class MapStyle:
    """How the layers are to be drawn — `render_map`'s styling arguments, as one value.

    Only the loop needs all of these together; the tool keeps its flat signature,
    because a nested options object makes the *call* harder for the model (the same
    reason `render_map` carries a ``noqa: PLR0913``).
    """

    column: str | None = None
    fields: list[str] | None = None
    scheme: str = "NaturalBreaks"
    k: int = 5
    cmap: str = "YlOrRd"
    legend: bool = True
    basemap: str = "OpenStreetMap"
    basemap_attribution: str = ""


@dataclass
class WmsSpec:
    """An OGC WMS to overlay live. Falsy when it is not fully specified.

    A WMS is pictures, not data — it can carry a map on its own, but nothing read
    from it is measurable.
    """

    url: str = ""
    layer: str = ""
    fmt: str = "image/png"
    attribution: str = ""

    def __bool__(self) -> bool:
        return bool(self.url and self.layer)


@dataclass
class MapBuild:
    """What the layer loop accumulated — the state, named instead of implied.

    Twelve values were carried through the loop as bare locals, several of them
    written in one branch and read hundreds of lines later. ``choro_k`` was the
    telling one: bound **only** inside the choropleth branch, so reading it on a
    plain map raised `UnboundLocalError` — the code worked around that with a
    comment rather than a default. As a field it simply starts at ``None``.
    """

    fmap: Any = None
    #: The map object exists but was made from a raster, so nothing zoomed to data.
    fmap_from_raster: bool = False
    total_vertices: int = 0
    drawn: list[str] = field(default_factory=list)
    drawn_resolved: list[str] = field(default_factory=list)  # absolute, for /qgis
    styling: dict[str, dict] = field(default_factory=dict)  # what was really drawn
    choro_applied: bool = False
    choro_k: int | None = None
    raster_drawn: bool = False
    raster_bounds: list[list[list[float]]] = field(default_factory=list)
    attributions: set[str] = field(default_factory=set)
    available_columns: set[str] = field(default_factory=set)  # union, for an error

    def note_licence(self, resolved: str) -> None:
        """Credit the layer's source, from its provenance sidecar.

        OSM and the open basemaps are licensed and must be named on the map.
        """
        meta = provenance.read_meta(resolved)
        if meta and meta.get("licence"):
            self.attributions.add(meta["licence"])

    def found_on_wms(self, wms: WmsSpec, style: MapStyle) -> None:
        """Make the map from the WMS alone, when no local layer drew one.

        No local layer means nothing set the extent, so centre on the WMS layer's
        advertised WGS84 bbox — one capabilities request, best-effort: an
        unreachable or mute service still yields a map, just a Germany-wide one.
        """
        import folium

        center, zoom = [51.0, 10.0], 6  # fallback: DE overview
        try:
            from owslib.wms import WebMapService

            for ver in ("1.3.0", "1.1.1"):
                try:
                    service = WebMapService(url=wms.url, version=ver)
                    bb = getattr(service.contents.get(wms.layer), "boundingBoxWGS84", None)
                    if bb:
                        center = [(bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2]
                        zoom = 10
                    break
                except Exception:  # noqa: BLE001 - try older version
                    continue
        except Exception:  # noqa: BLE001 - capabilities are best-effort
            pass
        self.fmap = folium.Map(
            location=center,
            zoom_start=zoom,
            tiles=style.basemap,
            attr=style.basemap_attribution or None,
        )

    def add_wms(self, wms: WmsSpec) -> None:
        """Overlay the WMS tiles on the existing map, and credit the service."""
        import folium

        folium.WmsTileLayer(
            url=wms.url,
            layers=wms.layer,
            fmt=wms.fmt,
            transparent=True,
            overlay=True,
            name=f"WMS: {wms.layer}",
            attr=wms.attribution or wms.url,
        ).add_to(self.fmap)
        if wms.attribution:
            self.attributions.add(wms.attribution)

    def decorate(self, title: str, attribution: str) -> None:
        """Layer control and the attribution caption. Purely cosmetic, never fatal.

        The map is the result; a missing legend box is not worth losing it, so
        every failure here is swallowed.
        """
        try:
            import folium

            folium.LayerControl().add_to(self.fmap)
            if title:
                folium.map.Marker(
                    [0, 0],
                    icon=folium.DivIcon(
                        html=f'<div style="font-weight:bold">{title}</div>'
                    ),
                )
            if attribution:
                # branca liefert keine Stubs fuer get_root().html
                self.fmap.get_root().html.add_child(  # type: ignore[attr-defined]
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

    def refuse_unmatched_column(self, column: str | None) -> dict | None:
        """A `column` that matched no vector layer — or ``None`` when it did.

        Only an error when no raster was drawn: `column` simply does not apply to
        a raster. The message spells out what `column` *is*, because the way it
        goes wrong is the model passing a field list to it.
        """
        if not column or self.choro_applied or self.raster_drawn:
            return None
        cols = ", ".join(sorted(self.available_columns)) or "(none)"
        return {
            "ok": False,
            "error": (
                f"column '{column}' not found in any layer. "
                f"`column` is a SINGLE column to colour a choropleth by "
                f"(usually numeric); it is not a list of popup fields — "
                f"pass those as `fields=[...]`. Available columns: {cols}"
            ),
        }

    def fit_to_rasters(self) -> None:
        """Zoom a raster-founded map onto its data.

        Measured 2026-09-07 (`swiss-terrain-slope-grindelwald`): the slope raster
        covered 1.1 x 0.8 km, the map opened at folium's default zoom 10 — some
        50 km of view with the raster a few pixels wide. The judges saw a correct
        computation and passed it; there was nothing to see on the map. Vector maps
        escape this because ``gdf.explore()`` zooms to its own data, which is why
        the pure-raster case stayed hidden so long.
        """
        if not (self.fmap_from_raster and self.raster_bounds):
            return
        south = min(b[0][0] for b in self.raster_bounds)
        west = min(b[0][1] for b in self.raster_bounds)
        north = max(b[1][0] for b in self.raster_bounds)
        east = max(b[1][1] for b in self.raster_bounds)
        try:
            self.fmap.fit_bounds([[south, west], [north, east]])
        except Exception:  # noqa: BLE001 — cosmetic, never fatal
            pass


def _add_raster(build: MapBuild, path: str, resolved: str, style: MapStyle) -> None:
    """A raster layer as a colourised ImageOverlay (WGS84).

    Not through the vector reader — it cannot open a GeoTIFF. A raster arriving
    first also has to build the map itself, since there is no ``gdf.explore()``
    to do it; that is what :attr:`MapBuild.fmap_from_raster` records.
    """
    import folium

    rgba, bounds, _scale = _raster_rgba_and_bounds(resolved, style.cmap)
    build.raster_bounds.append(bounds)
    if build.fmap is None:  # raster is the base — make the map ourselves
        center = [
            (bounds[0][0] + bounds[1][0]) / 2,
            (bounds[0][1] + bounds[1][1]) / 2,
        ]
        build.fmap = folium.Map(
            location=center,
            tiles=style.basemap,
            attr=style.basemap_attribution or None,
        )
        build.fmap_from_raster = True
    folium.raster_layers.ImageOverlay(
        image=rgba, bounds=bounds, opacity=0.75, name=path.split("/")[-1],
    ).add_to(build.fmap)
    build.raster_drawn = True


def _explore_kwargs(build: MapBuild, gdf, path: str, index: int, style: MapStyle) -> dict:
    """Decide how this layer is drawn — choropleth or a plain stacking colour."""
    kwargs: dict = {"m": build.fmap, "name": path.split("/")[-1]}
    column = style.column
    if column and column in gdf.columns:
        # Clamp class count to the distinct values present — a scheme like
        # NaturalBreaks fails when k exceeds them.
        use_k = max(1, min(style.k, int(gdf[column].nunique(dropna=True))))
        kwargs.update(
            column=column,
            scheme=style.scheme,
            k=use_k,
            cmap=style.cmap,
            legend=style.legend,
            style_kwds={"fillOpacity": 0.7},
        )
        build.choro_applied = True
        build.choro_k = use_k
    else:
        kwargs["color"] = _COLORS[index % len(_COLORS)]
        kwargs["style_kwds"] = {"fillOpacity": 0.3}

    n_points = int(gdf.geometry.geom_type.isin(("Point", "MultiPoint")).sum())
    if n_points:
        # Folium draws a point as a 10 px CircleMarker. An OSM road layer carries
        # thousands of nodes (crossings, signals), and at that radius they cover
        # every polygon below — the road-impact map of 2026-08-26 answered with
        # orange areas that no one could see under 4585 green dots.
        kwargs["marker_kwds"] = {"radius": 3 if n_points > 500 else 6}
    build.styling[path] = {
        "colour": kwargs.get("color", f"choropleth({style.cmap})"),
        "points_as_markers": n_points,
    }
    if build.fmap is None:  # the first layer sets the base tiles
        kwargs["tiles"] = style.basemap
        # Canvas rendering draws all features onto one <canvas> instead of one
        # DOM/SVG node each — the difference between "loads" and "usable" at
        # 100k+ features.
        kwargs["prefer_canvas"] = True
        if style.basemap_attribution:
            kwargs["attr"] = style.basemap_attribution
    return kwargs


def _add_vector(build: MapBuild, path: str, resolved: str, index: int,
                style: MapStyle) -> bool:
    """Draw one vector layer; ``False`` when it was empty and nothing was drawn."""
    import geopandas as gpd

    gdf = gpd.read_file(resolved)
    if gdf.empty:
        return False
    # Count vertices while the geometry is in memory anyway: `pyogrio.read_info`
    # knows the feature count, not the vertices — and those decide the render load.
    try:
        import shapely

        build.total_vertices += int(
            shapely.get_num_coordinates(gdf.geometry.values).sum()
        )
    except Exception:  # noqa: BLE001 — unknown must not block
        pass
    build.available_columns.update(c for c in gdf.columns if c != gdf.geometry.name)
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    # Keep only the columns that will be drawn/shown: geometry, the choropleth
    # column, and any requested `fields`. The embedded GeoJSON dominates the HTML
    # size, so dropping unused attribute columns is the single biggest size win.
    keep = set(style.fields or [])
    if style.column and style.column in gdf.columns:
        keep.add(style.column)
    gdf = gdf[[c for c in gdf.columns if c == gdf.geometry.name or c in keep]]

    build.fmap = gdf.explore(**_explore_kwargs(build, gdf, path, index, style))
    return True


def draw_layers(layers: list[str], workspace: str, style: MapStyle) -> MapBuild:
    """Draw every layer bottom-to-top onto one folium map, and report what happened.

    The return value is the whole state the rest of `render_map` needs: the map
    object, what was actually drawn, how it was styled, and the counts its guards
    act on. An empty layer is skipped silently — it is not a failure, it simply
    contributes nothing; whether *nothing at all* was drawn is the caller's call
    (a WMS can still carry the map).
    """
    build = MapBuild()
    for i, path in enumerate(layers):
        resolved = resolve_path(path, workspace)
        if _is_raster(path):
            _add_raster(build, path, resolved, style)
        elif not _add_vector(build, path, resolved, i, style):
            continue
        build.drawn.append(path)
        build.drawn_resolved.append(str(Path(resolved).resolve()))
        build.note_licence(resolved)
    return build


