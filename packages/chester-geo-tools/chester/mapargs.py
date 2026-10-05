"""What the model meant — the map tools' alias names, reconciled.

Phase KM, step 1.5. A concept of its own and therefore a module of its own: this is not
about maps but about the gap between what a tool declares and what a model types.
`render_map` **and** `inspect_map` need the same.

Every rule here comes from a run, not from a sense of style. Removing one means letting
that run fail again.
"""

from __future__ import annotations

import json


def as_list(value) -> list | None:
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

def normalise_args(layers, layer, fields, field, column, columns,
                   wms_url=None, wms_layer=None):
    """Reconcile the model's alias arguments onto the real parameters.

    Returns ``(layers, fields, column, error)``; ``error`` is either ``None`` or
    the finished refusal — there is nothing to draw.

    The `columns` branch is not cosmetics, it grew out of runs: the model reaches
    for `columns` (plural) as a *display-field list* and comma-joins it into one
    string ("shop,name") — but a choropleth is a single numeric column. Split the
    string, and if several names result they were meant as popup `fields`, not one
    choropleth: route them there instead of erroring on a bogus "shop,name" column
    name.
    """
    if not layers:
        layers = as_list(layer)
    if fields is None and field is not None:
        fields = as_list(field)
    if column is None and columns is not None:
        col = as_list(columns) or []
        if len(col) == 1 and "," in col[0]:
            col = [c.strip() for c in col[0].split(",") if c.strip()]
        if len(col) > 1:
            merged = list(fields) if fields else []
            merged += [c for c in col if c not in merged]
            fields = merged
        elif col:
            column = col[0]

    if not layers and not (wms_url and wms_layer):
        return layers, fields, column, {"ok": False, "error": "no layers given"}
    return layers or [], fields, column, None
