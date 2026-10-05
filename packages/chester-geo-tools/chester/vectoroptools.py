"""The eleven checked vector operations as tools — the wrappers around `geoops`.

Moved out of `vector.py` because that file stood at its baseline: here are only the
thin wrappers, the domain logic lives entirely in `chester/geoops.py`. The functions get
their checks there, and here their tool name and the text the model reads in the
catalogue.

**Why they are tools and not only names in the sandbox namespace.** Measured 2026-09-07
(`buffer-schools-500m`, QGIS off): the agent used `vector_split_by_geometry` unprompted
— it is in the tool catalogue — but never touched the same operations in the namespace
and wrote into its snippets "Since I can't call 'reproject' inside here" and
"Attempting to see if the tool 'reproject' is available in the scope". The instruction
claimed they were bound; the model did not believe it. What is in the catalogue gets
used; what is only in the prose does not. The run after that called `vector_reproject`
three times.

They also stay in the namespace of `geo_python_run`, for cases where several steps in
one snippet are cheaper than several tool calls.

They carry the prefix `vector_` because `buffer`, `clip` and `dissolve` would be too
generic as bare tool names; the eleven raster/terrain/network operations on
`GeoCoreCapability` have no prefix because `zonal_stats` and `service_area` are
unambiguous. In a snippet all twenty-one go by their short names.


**Moved on 2026-09-14** from `chester/capabilities/` to `chester/` (Phase KM, step 1):
the module was always framework-neutral — no `pydantic_ai`, no `selmakit` — but sat
below `capabilities/` and so fell out of `tests/test_structure.py::_pure_core_files`,
which checks only `chester/*.py`. Now the purity rule applies to it too.
"""

from __future__ import annotations

from collections.abc import Callable

from chester import geoops
from chester.vectortools import INSTRUCTIONS  # noqa: F401  # one text for the vector group


def build_tools(workspace: str) -> list[Callable]:
    """The eleven wrappers, bound to a workspace — in catalogue order.

    Named `build_tools` like every other wrapper module, and that is no cosmetic
    alignment: a server collecting `chester/*tools.py` calls exactly this name. As
    `op_tools` the module silently fell through the collection — ten checked vector
    operations were missing from the catalogue, without an error message (measured
    2026-09-14 on the first real FastMCP server).
    """
    ws = workspace

    def vector_reproject(input_path: str, output_path: str, target_crs: str) -> dict:
        """Transform a layer to ``target_crs`` (e.g. "EPSG:25832")."""
        return geoops.reproject(input_path, output_path, target_crs, workspace=ws)

    def vector_buffer(input_path: str, output_path: str, distance: float) -> dict:
        """Buffer every feature by ``distance`` in the layer's own units.

        Refuses a geographic CRS: there the distance is degrees, and the result
        would be wrong by orders of magnitude while looking plausible.
        """
        return geoops.buffer(input_path, output_path, distance, workspace=ws)

    def vector_clip(input_path: str, overlay_path: str, output_path: str) -> dict:
        """Cut ``input_path`` to the outline of ``overlay_path``.

        Keeps every geometry type the input holds — a mixed layer survives.
        """
        return geoops.clip(input_path, overlay_path, output_path, workspace=ws)

    def vector_intersection(input_path: str, overlay_path: str,
                            output_path: str) -> dict:
        """Geometric intersection, keeping the attributes of both layers."""
        return geoops.intersection(input_path, overlay_path, output_path, workspace=ws)

    def vector_extract_by_location(input_path: str, overlay_path: str,
                                   output_path: str,
                                   predicate: str = "intersects") -> dict:
        """Keep WHOLE features related to ``overlay_path`` (intersects/within/contains).

        Selection, not cutting: use `vector_clip` when the geometry should be cut
        at the border. Confusing the two is how an area comes out several times
        too large.
        """
        return geoops.extract_by_location(input_path, overlay_path, output_path,
                                          predicate=predicate, workspace=ws)

    def vector_extract_by_attribute(input_path: str, output_path: str,
                                    expression: str) -> dict:
        """Keep features matching a pandas query, e.g. "height > 15"."""
        return geoops.extract_by_attribute(input_path, output_path, expression,
                                           workspace=ws)

    def vector_dissolve(input_path: str, output_path: str,
                        by: str | None = None) -> dict:
        """Merge geometries, optionally grouped by a column."""
        return geoops.dissolve(input_path, output_path, by=by, workspace=ws)

    def vector_merge(input_paths: list[str], output_path: str) -> dict:
        """Stack several layers into ONE, bringing them onto a common CRS first.

        The counterpart of `vector_split_by_geometry`, and the right way to put split
        parts back together. Prefer it over a hand-rolled ``pd.concat``, which fails
        outright on two different CRSs and — worse — silently adopts the other
        layer's CRS for a layer that has none, leaving those coordinates
        untransformed. This reprojects instead, and reports which layers it had to
        move, which columns exist in only some of them, and whether the merge has
        re-created a mixed-geometry layer.
        """
        return geoops.merge(input_paths, output_path, workspace=ws)

    def vector_join(input_path: str, table_path: str, output_path: str,
                    field: str, table_field: str | None = None) -> dict:
        """Join a table (CSV or layer) onto a layer by a shared key column.

        ``field`` is the key in the layer, ``table_field`` the one in the table when
        it is named differently. Reports `joined` and `unjoined` and refuses to be
        quiet when rows found no match: a join matches on value AND type, and the
        integer 9375117 is not the text "09375117" — every Bavarian AGS begins with
        the state key 09, so a key read as a number silently matches nothing while
        the output still looks complete.
        """
        return geoops.join(input_path, table_path, output_path, field=field,
                           table_field=table_field, workspace=ws)

    def vector_add_field(input_path: str, output_path: str, name: str,
                         expression: str) -> dict:
        """Add a column computed from the existing ones; ``area``/``length`` are
        available as names and require a metric CRS."""
        return geoops.add_field(input_path, output_path, name, expression, workspace=ws)

    def vector_field_sum(input_path: str, column: str) -> dict:
        """Sum a numeric column — the answer, not a file.

        ``column`` may be "area" or "length"; both are then computed from the
        geometry and require a metric CRS.
        """
        return geoops.field_sum(input_path, column, workspace=ws)

    return [vector_reproject, vector_buffer, vector_clip, vector_intersection,
            vector_extract_by_location, vector_extract_by_attribute,
            vector_dissolve, vector_merge, vector_join, vector_add_field,
            vector_field_sum]
