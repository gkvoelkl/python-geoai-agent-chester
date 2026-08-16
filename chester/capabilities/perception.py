"""PerceptionCapability — extract information from imagery via spectral indices.

This is the "AI perception" pillar in its lightweight, fully local
form: normalized-difference indices (NDWI for water, NDVI for vegetation) computed
with rasterio + numpy, then thresholded and polygonized into vector features. It
needs no GPU and no model download, and maps directly onto the classic
"NDVI / band combinations" perception tools.

Heavier learned perception (SAM/`samgeo`, Prithvi) is a documented future extension
— see the note in TODO.md. The tool interface here
(raster in → vector mask out) is the same shape those would slot into.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester import provenance
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

_INSTRUCTIONS = """\
## Perception (spectral indices)

Turn multispectral bands into features:
- `detect_water(green, nir, ...)` → NDWI = (green − nir)/(green + nir); pixels above
  the threshold (default 0) are water. Returns a water mask raster and, optionally,
  water polygons. Use for flood / water mapping from Sentinel-2 (assets 'green' and
  'nir').
- `spectral_index(band_a, band_b, kind=...)` → generic normalized difference. ndwi =
  (green, nir); ndvi = (nir, red). Returns the index raster and its value range.

Bands must share the same grid/CRS (Sentinel-2 10 m bands do). Results are in the
input raster's CRS.\
"""


def _read_band(path: str):
    import rasterio

    with rasterio.open(path) as ds:
        arr = ds.read(1).astype("float32")
        return arr, ds.profile, ds.transform, ds.crs


def _normalized_difference(a, b):
    import numpy as np

    denom = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(denom != 0, (a - b) / denom, 0.0)
    return out.astype("float32")


@dataclass
class PerceptionCapability(AbstractCapability[Any]):
    """Spectral-index perception: water (NDWI) and vegetation (NDVI)."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace

        def spectral_index(
            band_a: str, band_b: str, output_path: str, kind: str = "ndwi"
        ) -> dict:
            """Compute a normalized-difference index raster from two bands.

            (A − B)/(A + B). For ndwi pass band_a=green, band_b=nir; for ndvi pass
            band_a=nir, band_b=red. Writes a float32 index raster (range −1..1) and
            returns its min/mean/max.
            """
            try:
                import rasterio

                output_path = resolve_path(output_path, ws)
                a, profile, _, _ = _read_band(resolve_path(band_a, ws))
                b, _, _, _ = _read_band(resolve_path(band_b, ws))
                if a.shape != b.shape:
                    return {"ok": False, "error": f"band shape mismatch {a.shape} vs {b.shape}"}
                idx = _normalized_difference(a, b)
                profile.update(dtype="float32", count=1, nodata=None)
                with rasterio.open(output_path, "w", **profile) as ds:
                    ds.write(idx, 1)
                provenance.write_meta(
                    output_path, source="chester", tool="spectral_index",
                    query={"kind": kind},
                )
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return {
                "ok": True,
                "kind": kind,
                "output": output_path,
                "range": [round(float(idx.min()), 4), round(float(idx.max()), 4)],
                "mean": round(float(idx.mean()), 4),
            }

        def detect_water(
            green: str,
            nir: str,
            mask_path: str,
            threshold: float = 0.0,
            polygons_path: str | None = None,
        ) -> dict:
            """Map open water from green and NIR bands using NDWI.

            NDWI = (green − nir)/(green + nir); pixels > threshold are classified as
            water. Writes a uint8 water mask (1=water, 0=other) to mask_path. If
            polygons_path is given, also vectorizes the water areas to that file and
            returns the polygon count.
            """
            try:
                import rasterio

                mask_path = resolve_path(mask_path, ws)
                if polygons_path:
                    polygons_path = resolve_path(polygons_path, ws)
                g, profile, transform, crs = _read_band(resolve_path(green, ws))
                n, _, _, _ = _read_band(resolve_path(nir, ws))
                if g.shape != n.shape:
                    return {"ok": False, "error": f"band shape mismatch {g.shape} vs {n.shape}"}
                ndwi = _normalized_difference(g, n)
                water = (ndwi > threshold).astype("uint8")

                profile.update(dtype="uint8", count=1, nodata=0)
                with rasterio.open(mask_path, "w", **profile) as ds:
                    ds.write(water, 1)
                provenance.write_meta(
                    mask_path, source="chester", tool="detect_water",
                    query={"threshold": threshold},
                )

                water_fraction = float(water.mean())
                result = {
                    "ok": True,
                    "mask": mask_path,
                    "threshold": threshold,
                    "water_fraction": round(water_fraction, 4),
                }

                if polygons_path:
                    import geopandas as gpd
                    from rasterio.features import shapes
                    from shapely.geometry import shape

                    geoms = [
                        shape(geom)
                        for geom, val in shapes(water, mask=water.astype(bool), transform=transform)
                        if val == 1
                    ]
                    if not geoms:
                        result["polygons"] = None
                        result["polygon_count"] = 0
                    else:
                        gdf = gpd.GeoDataFrame(geometry=geoms, crs=crs)
                        gdf.to_file(polygons_path)
                        provenance.write_meta(
                            polygons_path, source="chester", tool="detect_water",
                            query={"threshold": threshold},
                        )
                        result["polygons"] = polygons_path
                        result["polygon_count"] = len(gdf)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return result

        return FunctionToolset(tools=[detect_water, spectral_index])
