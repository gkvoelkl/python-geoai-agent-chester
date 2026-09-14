"""Gelände- und Luftbildbezug (DE/CH/AT) als **rahmenneutrale** Hüllen.

Phase KM, Schritt 1. Sechs Werkzeuge, die dasselbe Bedürfnis in absteigender Auflösung
bedienen: `fetch_dgm1` (1 m, amtlich DE), `fetch_swissalti3d` (CH), `fetch_austria_dem`
(AT), `fetch_dem` (Copernicus GLO-30, überall) — dazu `fetch_dop` für Luftbilder und
`fetch_swisstlmregio` für den Schweizer Vektorbestand. `_with_coverage` gehört hierher,
weil jede dieser Beschaffungen melden muss, **wieviel** der angefragten Fläche sie
tatsächlich abgedeckt hat; eine Kachel weniger ist sonst nicht zu sehen.
"""

from __future__ import annotations

from collections.abc import Callable

from chester import provenance
from chester.workspace import resolve_path

_DEM_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"

_DEM_LICENCE = "Copernicus DEM GLO-30 © ESA / DLR / Airbus (free, full open licence)"

def _glo30_tile_name(lat: int, lon: int) -> str:
    """GLO-30 tile id for the 1°×1° cell whose SW corner is (lat, lon)."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"

def _glo30_tiles(bbox: list[float]) -> list[tuple[str, str]]:
    """(url, tile_name) for every GLO-30 tile covering ``[west, south, east, north]``.

    A tile covers ``[lon, lon+1) × [lat, lat+1)``, so the covering integer
    corners run from ``floor(west/south)`` up to ``ceil(east/north) − 1``; a
    degenerate (zero-width) bbox still yields its containing tile.
    """
    import math

    west, south, east, north = bbox
    lon_lo, lon_hi = math.floor(west), max(math.ceil(east), math.floor(west) + 1)
    lat_lo, lat_hi = math.floor(south), max(math.ceil(north), math.floor(south) + 1)
    tiles = []
    for lat in range(lat_lo, lat_hi):
        for lon in range(lon_lo, lon_hi):
            name = _glo30_tile_name(lat, lon)
            tiles.append((f"{_DEM_BASE}/{name}/{name}.tif", name))
    return tiles

def _with_coverage(result: dict, path: str, bbox: list[float] | None) -> dict:
    """Add "how much of the request does this raster actually cover" to a result.

    A download reaching over 60 % of the asked-for area returns `ok: true` like any
    other, and every mean or sum computed afterwards is quietly based on the part
    that arrived. Li, Ning et al. (2025) count exactly this — "Does it adequately
    cover the study area?" — among the uncertainties a data-aware system must
    resolve rather than pass on.
    """
    from chester.geofacts import coverage_warning, raster_coverage

    cov = raster_coverage(path, bbox)
    if not cov:
        return result
    result["coverage"] = cov
    note = coverage_warning(cov)
    if note:
        result["warning"] = f"{result['warning']} {note}" if result.get("warning") else note
    return result


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """Die 6 Werkzeuge dieser Gruppe, an ``workspace`` gebunden."""
    ws = workspace

    def fetch_dem(bbox: list[float], output_path: str) -> dict:
        """Download a bbox of the Copernicus DEM GLO-30 (~30 m) as a GeoTIFF.

        Elevation for ``bbox`` = [west, south, east, north] in WGS84. Mosaics
        the public Copernicus GLO-30 tiles covering the area and writes a
        single-band GeoTIFF (in EPSG:4326, **degrees**). Reproject to a metric
        CRS before computing slope/area. Use this when a workflow needs terrain
        and no DEM was provided. Returns the local path, size and tiles used.
        """
        try:
            import rasterio
            from rasterio.merge import merge

            output_path = resolve_path(output_path, ws, write=True)
            west, south, east, north = bbox
            gdal_env = rasterio.Env(
                GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                GDAL_HTTP_MULTIRANGE="YES",
                CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
            )
            datasets, missing = [], []
            with gdal_env:
                for url, name in _glo30_tiles(bbox):
                    try:
                        datasets.append(rasterio.open(url))
                    except Exception:  # noqa: BLE001 - ocean/absent tile: skip
                        missing.append(name)
                if not datasets:
                    return {
                        "ok": False,
                        "error": "no Copernicus DEM tiles cover the bbox (open water?)",
                        "tiles_missing": missing,
                    }
                try:
                    mosaic, transform = merge(datasets, bounds=(west, south, east, north))
                    crs = str(datasets[0].crs)
                    profile = datasets[0].profile
                finally:
                    for ds in datasets:
                        ds.close()
                profile.update(
                    driver="GTiff",
                    count=1,
                    height=mosaic.shape[1],
                    width=mosaic.shape[2],
                    transform=transform,
                )
                with rasterio.open(output_path, "w", **profile) as dst:
                    dst.write(mosaic[0], 1)
            provenance.write_meta(
                output_path,
                source="connector/copernicus-dem",
                tool="fetch_dem",
                query={"bbox": bbox},
                crs=crs,
                licence=_DEM_LICENCE,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result = {
            "ok": True,
            "output": output_path,
            "size": [int(mosaic.shape[2]), int(mosaic.shape[1])],
            "crs": crs,
            "tiles_used": len(datasets),
            "resolution_m": 30,
            "note": "GLO-30 elevation in EPSG:4326 (degrees) — reproject to a "
            "metric CRS before slope/area calculations.",
        }
        return _with_coverage(result, output_path, bbox)

    def fetch_dgm1(bbox: list[float], output_path: str, state: str | None = None) -> dict:
        """Download open **1 m** terrain (DGM1) for a bbox as a GeoTIFF.

        The high-resolution sibling of ``fetch_dem``: where ``fetch_dem`` is
        Copernicus GLO-30 (~30 m, degrees), this is the Bundesländer's open
        **1 m** DGM in a **metric** CRS (EPSG:25832) — so slope/area work
        directly, and it can serve as the DTM half of a DSM−DTM building
        height. ``bbox`` = [west, south, east, north] in WGS84; the state
        auto-detects (or pass ``state`` = "BY"/"NW"/"BB"/"MV"). Wired: Bayern,
        NRW, Brandenburg, Mecklenburg-Vorpommern (the BKG's nationwide DGM1
        needs a registered token, so it is not used). Prefer this over
        ``fetch_dem`` for fine terrain wherever it is covered.
        """
        from chester import dgm1

        output_path = resolve_path(output_path, ws, write=True)
        tile_cache = str(resolve_path("_dgm1_tiles", ws))
        try:
            r = dgm1.fetch_dgm1(bbox, output_path, tile_cache, state=state)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if r.get("ok"):
            provenance.write_meta(
                output_path,
                source=f"connector/dgm1-{r['state'].lower()}",
                tool="fetch_dgm1",
                query={"bbox": bbox, "state": r["state"]},
                crs=r.get("crs"),
                licence=r.get("licence"),
            )
            return _with_coverage(r, output_path, bbox)
        return r

    def fetch_dop(bbox: list[float], output_path: str, state: str | None = None) -> dict:
        """Download an open aerial orthophoto (DOP) for a bbox as a GeoTIFF.

        The **imagery** sibling of ``fetch_dgm1``: the Bundesländer's open
        DOP, mosaicked over ``bbox`` = [west, south, east, north] in WGS84
        into a multi-band GeoTIFF in a metric CRS (EPSG:25832/25833). Wired:
        NRW (10 cm), Brandenburg, Mecklenburg-Vorpommern and Bayern (20 cm).
        All but Bayern are **RGBI**, so band 4 is near infrared and
        ``spectral_index`` computes NDVI at that resolution with
        ``band_a_index=4`` (NIR) and ``band_b_index=1`` (red) — Bayern is RGB
        only, so check ``has_nir`` in the result before planning an NDVI step;
        without NIR the honest answer is that NDVI is not computable here.
        ``state`` pins the source ("NW"/"BB"/"MV"/"BY") instead of
        auto-detecting.

        Unlike ``fetch_wms_map`` — a rendered picture that must never be
        analysed — this is image **data** with defined radiometry: use it for
        NDVI/classification, as an orthophoto backdrop for maps and visual
        checks, and as ground texture under 3D buildings. Tiles are large
        (18-83 MB each), so keep the bbox small.
        """
        from chester import dop

        output_path = resolve_path(output_path, ws, write=True)
        tile_cache = str(resolve_path("_dop_tiles", ws))
        try:
            r = dop.fetch_dop(bbox, output_path, tile_cache, state=state)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if r.get("ok"):
            provenance.write_meta(
                output_path,
                source=f"connector/dop-{r['state'].lower()}",
                tool="fetch_dop",
                query={"bbox": bbox, "state": r["state"]},
                crs=r.get("crs"),
                licence=r.get("licence"),
                # Aerial imagery: when the picture was taken matters as much as
                # what it shows. Only NRW states it in the tile name.
                acquired=r.get("acquired"),
            )
            return _with_coverage(r, output_path, bbox)
        return r

    def fetch_swissalti3d(bbox: list[float], output_path: str, resolution: float = 2.0) -> dict:
        """Download the **Swiss** high-res terrain (swissALTI3D) for a bbox as a GeoTIFF.

        The Switzerland counterpart of ``fetch_dgm1``: the swisstopo open DTM
        (``resolution`` 2 m default, or 0.5 m) mosaicked over ``bbox`` = [west,
        south, east, north] in WGS84, in **EPSG:2056 (LV95, metres)** — so
        slope/area work directly. Switzerland only; for German terrain use
        ``fetch_dgm1``, elsewhere ``fetch_dem``.
        """
        from chester import swisstopo

        output_path = resolve_path(output_path, ws, write=True)
        try:
            r = swisstopo.fetch_swissalti3d(bbox, output_path, resolution=resolution)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if r.get("ok"):
            provenance.write_meta(
                output_path,
                source="connector/swissalti3d",
                tool="fetch_swissalti3d",
                query={"bbox": bbox, "resolution": resolution},
                crs=r.get("crs"),
                licence=r.get("licence"),
            )
        return r

    def fetch_austria_dem(bbox: list[float], output_path: str) -> dict:
        """Download the **Austrian** 1 m terrain (BEV ALS DGM) for a bbox as a GeoTIFF.

        The Austria counterpart of ``fetch_dgm1`` (DE) / ``fetch_swissalti3d`` (CH):
        window-reads the covering BEV ALS Cloud-Optimized GeoTIFF tiles over ``bbox``
        = [west, south, east, north] in WGS84 and mosaics them into one GeoTIFF in
        **EPSG:3035 (metres)** — so slope/area work directly. Austria only; nodata is
        -9999. For German terrain use ``fetch_dgm1``, Swiss ``fetch_swissalti3d``,
        elsewhere ``fetch_dem``.
        """
        from chester import austria

        output_path = resolve_path(output_path, ws, write=True)
        cache_dir = str(resolve_path("_at_dgm", ws))
        try:
            r = austria.fetch_austria_dem(bbox, output_path, cache_dir)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if r.get("ok"):
            provenance.write_meta(
                output_path,
                source="connector/bev-als",
                tool="fetch_austria_dem",
                query={"bbox": bbox},
                crs=r.get("crs"),
                licence=r.get("licence"),
            )
        return r

    def fetch_swisstlmregio(
        theme: str, output_path: str, bbox: list[float] | None = None
    ) -> dict:
        """Download **Swiss** topographic vector (swissTLMRegio) for a theme as a GeoPackage.

        ``theme`` is one of roads / railways / buildings / landcover / lakes /
        rivers / builtup / poi / names. ``bbox`` = [west, south, east, north] in
        WGS84 windows it (recommended — some layers are national and large).
        Output is EPSG:2056 (LV95). swissTLMRegio is the generalised (≈1:200 000)
        authoritative Swiss topographic model; the full-resolution swissTLM3D has
        no per-bbox route, so for finer Swiss topographic detail use
        ``osm_features``. Switzerland only.
        """
        from chester import swisstopo

        output_path = resolve_path(output_path, ws, write=True)
        cache_dir = str(resolve_path("_tlmregio", ws))
        try:
            r = swisstopo.fetch_swisstlmregio(theme, output_path, cache_dir, bbox_wgs84=bbox)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if r.get("ok"):
            provenance.write_meta(
                output_path,
                source="connector/swisstlmregio",
                tool="fetch_swisstlmregio",
                query={"theme": theme, "bbox": bbox},
                crs=r.get("crs"),
                licence=r.get("licence"),
            )
        return r

    return [fetch_dem, fetch_dgm1, fetch_dop,
            fetch_swissalti3d, fetch_austria_dem, fetch_swisstlmregio]
