"""GeoLod2Capability — authoritative German building heights from open LoD2.

Rahmenneutrale Hüllen (Phase KM, Schritt 1): Werkzeuge einmal beschrieben,
zwei Adapter — `capabilities/lod2.py` für Chesters Agenten, später der
MCP-Server. Kein `pydantic_ai`, kein `selmakit`.
"""

from __future__ import annotations

from collections.abc import Callable

from chester import lod2, provenance
from chester.workspace import resolve_path

INSTRUCTIONS = """\
## Building heights — use open LoD2, not a global DEM

For **building heights** (how tall a building is), the authoritative source is the
Bundesländer's open **LoD2** 3D building models: every building carries a
laser-measured height. Prefer this over DSM−DTM differencing, and **never** use
`fetch_dem` (Copernicus GLO-30, ~30 m) for building heights — it cannot resolve a
building.

- `lod2_sources()` — which Bundesländer are wired (fetchable now) vs. documented
  (open, but access not yet wired). Wired today: Bayern (BY), Nordrhein-Westfalen
  (NW), Brandenburg (BB), Mecklenburg-Vorpommern (MV). For a documented-only
  state, tell the user the open portal from the list.
- `fetch_lod2(bbox, output_path, state?, street?)` — fetch buildings for a WGS84
  bbox = [west, south, east, north]. The Bundesland is auto-detected from the bbox
  (or pass `state`, e.g. "BY"). Optional `street` keeps only that street's
  buildings. Output is a GeoPackage with a **`measured_height`** column (metres)
  plus `street`/`housenumber`, in a metric CRS (EPSG:25832/25833) — heights and
  areas are already in metres, so no reprojection is needed before stats.

Typical flow: `geocode` → `fetch_lod2(bbox, ..., street=…)` → analyse
`measured_height` (e.g. `vector_field_sum`, or a Gini) → `render_map(column=
"measured_height")`. Validate with `sanity_check_result` before reporting.\
"""


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """Die Werkzeuge dieser Gruppe, an ``workspace`` gebunden."""
    ws = workspace

    def lod2_sources() -> dict:
        """List per-Bundesland open LoD2 coverage: wired (fetchable now) vs.
        documented (open, but portal access not yet wired), with licences."""
        states = []
        for s in lod2.BUNDESLAENDER.values():
            states.append({
                "code": s.code, "name": s.name, "status": s.status,
                "crs": f"EPSG:{s.epsg}", "portal": s.portal, "licence": s.licence,
            })
        wired = [s["code"] for s in states if s["status"] == "open"]
        return {"ok": True, "wired": wired, "count": len(states), "sources": states,
                "note": "Baden-Württemberg is not listed — LGL BW does not "
                "publish LoD2 as open data."}

    def fetch_lod2(
        bbox: list[float],
        output_path: str,
        state: str | None = None,
        street: str | None = None,
    ) -> dict:
        """Fetch authoritative LoD2 building heights for a bbox into a GeoPackage.

        ``bbox`` = [west, south, east, north] in WGS84. The Bundesland is
        auto-detected from the bbox unless ``state`` (e.g. "BY", "NW") is given.
        ``street`` keeps only buildings on that street (prefix match). Downloads
        the covering CityGML tiles, parses footprint + laser-measured height +
        address, clips to the bbox, and writes a GeoPackage with a
        **``measured_height``** column (metres) in a metric CRS — the true,
        per-building height. Use this for building heights instead of
        ``fetch_dem``/DSM−DTM. Returns counts, height stats, CRS and licence.

        **The geometry is a flat footprint** — a height *number* per building,
        not a 3D shell. For a 3D view call ``fetch_cityjson`` on the same bbox
        instead (same tiles, real roof geometry).
        """
        output_path = resolve_path(output_path, ws, write=True)
        tile_cache = str(resolve_path("_lod2_tiles", ws))
        try:
            r = lod2.fetch_lod2(bbox, output_path, tile_cache,
                                state=state, street=street)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if r.get("ok"):
            provenance.write_meta(
                output_path, source=f"connector/lod2-{r['state'].lower()}",
                tool="fetch_lod2",
                query={"bbox": bbox, "state": r["state"], "street": street},
                crs=r.get("crs"), licence=r.get("licence"),
            )
            # The signpost back out of a dead end. A run that asked for the
            # height *and* a 3D view fetched only this, found no 3D input,
            # correctly diagnosed "needs CityJSON" — and stopped there instead
            # of re-fetching (2026-08-19, `city3d-regensburg-dom-height`). The
            # tiles are already cached by then, so the second fetch is cheap.
            r["geometry"] = "flat footprints + measured_height (no 3D shells)"
            r["for_3d_use"] = (
                "fetch_cityjson(bbox, output_path) on this same bbox — same "
                "CityGML tiles, real LoD2 roof geometry, and the input that "
                "render_buildings_3d and qgis_show_3d actually need."
            )
        return r

    return [lod2_sources, fetch_lod2]
