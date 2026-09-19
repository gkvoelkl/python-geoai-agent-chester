"""PerceptionCapability — extract information from imagery via spectral indices.

Rahmenneutrale Hüllen (Phase KM, Schritt 1): Werkzeuge einmal beschrieben,
zwei Adapter — `capabilities/perception.py` für Chesters Agenten, später der
MCP-Server. Kein `pydantic_ai`, kein `selmakit`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from chester import provenance
from chester.workspace import resolve_path

INSTRUCTIONS = """\
## Perception (spectral indices)

Turn multispectral bands into features:
- `detect_water(green, nir, ...)` → NDWI = (green − nir)/(green + nir); pixels above
  the threshold (default 0) are water. Returns a water mask raster and, optionally,
  water polygons. Use for flood / water mapping from Sentinel-2 (assets 'green' and
  'nir').
- `spectral_index(band_a, band_b, kind=...)` → generic normalized difference. ndwi =
  (green, nir); ndvi = (nir, red). Returns the index raster and its value range.

The two bands may be separate single-band files (Sentinel-2 assets) or two bands of
one composite — address those with `band_a_index` / `band_b_index` (RGBI orthophoto:
NIR is band 4, red is band 1, so NDVI is band_a_index=4, band_b_index=1).

**NDVI needs a near-infrared band; it cannot be derived from RGB.** Naming the same
band twice is refused, and so is an NDVI over a source whose bands do not include
NIR. If an image has no NIR, say so — do not substitute a visible band.

Bands must share the same grid/CRS (Sentinel-2 10 m bands do). Results are in the
input raster's CRS.\
"""


def _read_band(path: str, index: int = 1):
    import rasterio

    with rasterio.open(path) as ds:
        if not 1 <= index <= ds.count:
            raise ValueError(
                f"band {index} does not exist in {Path(path).name} — the file has "
                f"{ds.count} band(s)"
            )
        arr = ds.read(index).astype("float32")
        return arr, ds.profile, ds.transform, ds.crs


def _source_bands(path: str) -> tuple[int, tuple]:
    import rasterio

    with rasterio.open(path) as ds:
        return ds.count, tuple(d or "" for d in ds.descriptions)


def _missing_nir(kind: str, path: str) -> dict | None:
    """Refuse an NDVI over a composite that carries no near-infrared band.

    Only asked when both bands are read from the *same* file: then the band count is
    the whole truth about what the image holds, and a three-band RGB orthophoto
    demonstrably has no NIR. Two separate single-band files (the Sentinel-2 shape)
    say nothing about each other and are left to the caller.

    A three-band NIR/red/green stack is legitimate, so a band description naming NIR
    lifts the refusal — the data has to state it, which is the point.
    """
    if "ndvi" not in kind.lower():
        return None
    count, descriptions = _source_bands(path)
    if count >= 4 or any("nir" in d.lower() for d in descriptions):
        return None
    return {
        "ok": False,
        "error": (
            f"{Path(path).name} has {count} band(s) and none is declared near "
            f"infrared, so NDVI cannot be computed from it — NDVI is "
            f"(NIR − red)/(NIR + red) and no visible band substitutes for NIR. "
            f"Report that this image has no NIR instead of computing an index."
        ),
        "has_nir": False,
    }


def _normalized_difference(a, b):
    import numpy as np

    denom = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(denom != 0, (a - b) / denom, 0.0)
    return out.astype("float32")


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """Die Werkzeuge dieser Gruppe, an ``workspace`` gebunden."""
    ws = workspace

    def spectral_index(
        band_a: str,
        band_b: str,
        output_path: str,
        kind: str = "ndwi",
        band_a_index: int = 1,
        band_b_index: int = 1,
    ) -> dict:
        """Compute a normalized-difference index raster from two bands.

        (A − B)/(A + B). For ndwi pass band_a=green, band_b=nir; for ndvi pass
        band_a=nir, band_b=red. The bands may be separate single-band files or two
        bands of one composite — then set band_a_index/band_b_index (RGBI
        orthophoto: NIR is 4, red is 1). Writes a float32 index raster (range
        −1..1) and returns its min/mean/max. Refuses an NDVI over a source without
        a near-infrared band rather than inventing one.
        """
        try:
            import rasterio

            output_path = resolve_path(output_path, ws, write=True)
            path_a = resolve_path(band_a, ws)
            path_b = resolve_path(band_b, ws)
            same_file = Path(path_a) == Path(path_b)
            # Order matters. "No NIR in this file" is the terminal answer, so it
            # comes first: told only "same band twice", a model retries with other
            # indices and burns its budget discovering the same dead end.
            if same_file and (refusal := _missing_nir(kind, path_a)) is not None:
                return refusal
            # One band against itself is 0 for every pixel — a raster that looks
            # like a result and carries no information. The probe that found this
            # got a plausible float32 GeoTIFF of pure zeros back, with ok: true.
            if same_file and band_a_index == band_b_index:
                return {
                    "ok": False,
                    "error": (
                        f"band_a and band_b are both band {band_a_index} of "
                        f"{Path(path_a).name} — (A − B)/(A + B) is 0 for every "
                        f"pixel. Name the two different bands the index is "
                        f"defined on, via band_a_index/band_b_index."
                    ),
                }
            a, profile, _, _ = _read_band(path_a, band_a_index)
            b, _, _, _ = _read_band(path_b, band_b_index)
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
        green_index: int = 1,
        nir_index: int = 1,
    ) -> dict:
        """Map open water from green and NIR bands using NDWI.

        NDWI = (green − nir)/(green + nir); pixels > threshold are classified as
        water. Writes a uint8 water mask (1=water, 0=other) to mask_path. If
        polygons_path is given, also vectorizes the water areas to that file and
        returns the polygon count. Set green_index/nir_index when both bands live
        in one composite (RGBI orthophoto: green is 2, NIR is 4).
        """
        try:
            import rasterio

            mask_path = resolve_path(mask_path, ws)
            if polygons_path:
                polygons_path = resolve_path(polygons_path, ws)
            path_g = resolve_path(green, ws)
            path_n = resolve_path(nir, ws)
            # Same reason as in spectral_index: identical inputs make NDWI 0
            # everywhere, which thresholds to "no water" and looks like an answer.
            if Path(path_g) == Path(path_n) and green_index == nir_index:
                return {
                    "ok": False,
                    "error": (
                        f"green and nir are both band {green_index} of "
                        f"{Path(path_g).name} — NDWI is 0 for every pixel. Name "
                        f"the green and the near-infrared band separately."
                    ),
                }
            g, profile, transform, crs = _read_band(path_g, green_index)
            n, _, _, _ = _read_band(path_n, nir_index)
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

    return [detect_water, spectral_index]
