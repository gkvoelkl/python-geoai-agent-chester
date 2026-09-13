"""Tests for the spectral-index tools — above all what they must refuse.

The probe `ndvi-without-nir` caught `spectral_index` computing an "NDVI" from a
three-band RGB orthophoto: the agent passed the same file as both bands, the tool
returned ok with a float32 GeoTIFF of pure zeros, and a provenance sidecar declared
it an NDVI. Two holes met there — no band index existed, so a composite's band 4 was
unreachable at all, and identical inputs were not checked. Both are covered here,
plus the Sentinel-2 shape (two separate single-band files) that must keep working.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")

from chester.capabilities.perception import PerceptionCapability  # noqa: E402


def _write(path, bands, descriptions=None):
    """Write a tiny GeoTIFF; `bands` is a list of 2-D uint16 arrays."""
    from rasterio.transform import from_origin

    profile = {
        "driver": "GTiff",
        "height": bands[0].shape[0],
        "width": bands[0].shape[1],
        "count": len(bands),
        "dtype": "uint16",
        "crs": "EPSG:25832",
        "transform": from_origin(700000, 5400000, 1.0, 1.0),
    }
    with rasterio.open(path, "w", **profile) as ds:
        for i, arr in enumerate(bands, start=1):
            ds.write(arr, i)
        if descriptions:
            ds.descriptions = tuple(descriptions)
    return str(path)


def _tools(ws):
    toolset = PerceptionCapability(workspace=str(ws)).get_toolset()
    return {t.name: t.function for t in toolset.tools.values()}


@pytest.fixture
def tools(tmp_path):
    return _tools(tmp_path)


def _rgb(tmp_path, name="aerial_rgb.tif"):
    rng = np.random.default_rng(0)
    bands = [rng.integers(40, 210, size=(8, 8), dtype="uint16") for _ in range(3)]
    return _write(tmp_path / name, bands)


def test_ndvi_from_rgb_is_refused_and_writes_nothing(tmp_path, tools):
    src = _rgb(tmp_path)
    out = tmp_path / "probe_ndvi.tif"

    res = tools["spectral_index"](
        band_a=src, band_b=src, output_path=str(out), kind="ndvi"
    )

    assert res["ok"] is False
    assert "NIR" in res["error"] or "near infrared" in res["error"]
    # The refusal is the whole point: no raster, and no sidecar claiming an NDVI.
    assert not out.exists()
    assert not (tmp_path / "probe_ndvi.tif.meta.json").exists()


def test_same_band_twice_is_refused_before_a_zero_raster_is_written(tmp_path, tools):
    # kind=ndwi, so the NIR guard cannot be what refuses this one.
    src = _rgb(tmp_path)
    out = tmp_path / "ndwi.tif"

    res = tools["spectral_index"](
        band_a=src, band_b=src, output_path=str(out), kind="ndwi",
        band_a_index=2, band_b_index=2,
    )

    assert res["ok"] is False
    assert "band 2" in res["error"]
    assert not out.exists()


def test_band_index_out_of_range_names_the_band_count(tmp_path, tools):
    src = _rgb(tmp_path)

    res = tools["spectral_index"](
        band_a=src, band_b=src, output_path=str(tmp_path / "x.tif"), kind="ndwi",
        band_a_index=4, band_b_index=1,
    )

    assert res["ok"] is False
    assert "3 band" in res["error"]  # says what the file actually has


def test_ndvi_from_an_rgbi_composite_uses_band_four(tmp_path, tools):
    rng = np.random.default_rng(1)
    red = rng.integers(40, 120, size=(8, 8), dtype="uint16")
    nir = rng.integers(400, 900, size=(8, 8), dtype="uint16")
    src = _write(
        tmp_path / "dop_rgbi.tif",
        [red, rng.integers(40, 120, size=(8, 8), dtype="uint16"),
         rng.integers(40, 120, size=(8, 8), dtype="uint16"), nir],
    )
    out = tmp_path / "ndvi.tif"

    res = tools["spectral_index"](
        band_a=src, band_b=src, output_path=str(out), kind="ndvi",
        band_a_index=4, band_b_index=1,
    )

    assert res["ok"] is True
    # Seit 2026-09-13 wird eine **Ausgabe** in den GeoCache gezwungen, auch wenn ein
    # absoluter Pfad kommt (`resolve_path(..., write=True)`): Eine Datei ausserhalb des
    # Caches hat keinen Inventareintrag, keinen Touch-on-Read-Schutz und keine TTL — und
    # das Gate findet sie nicht. Geprüft wird deshalb der **zurückgegebene** Pfad; das
    # ist ohnehin die harte Regel („a writing tool returns its output path").
    written = Path(res["output"])
    assert written.exists()
    assert written.name == out.name
    assert "geocache" in written.parts, f"Ausgabe ausserhalb des Caches: {written}"
    expected = (nir.astype("float32") - red) / (nir.astype("float32") + red)
    with rasterio.open(written) as ds:
        assert np.allclose(ds.read(1), expected, atol=1e-6)
    # Vegetation NIR >> red, so the index is well clear of zero — the defect signature
    # that started this was an all-zero raster.
    assert res["mean"] > 0.5


def test_three_band_stack_that_declares_nir_is_allowed(tmp_path, tools):
    rng = np.random.default_rng(2)
    nir = rng.integers(400, 900, size=(8, 8), dtype="uint16")
    red = rng.integers(40, 120, size=(8, 8), dtype="uint16")
    src = _write(
        tmp_path / "stack.tif",
        [nir, red, rng.integers(40, 120, size=(8, 8), dtype="uint16")],
        descriptions=["NIR", "red", "green"],
    )

    res = tools["spectral_index"](
        band_a=src, band_b=src, output_path=str(tmp_path / "ndvi2.tif"), kind="ndvi",
        band_a_index=1, band_b_index=2,
    )

    assert res["ok"] is True, res  # the data states it has NIR, so the guard lifts


def test_separate_single_band_files_still_work(tmp_path, tools):
    # The Sentinel-2 shape: two files say nothing about each other, so the NIR guard
    # must not fire — it would break every STAC-driven NDVI.
    rng = np.random.default_rng(3)
    nir = rng.integers(400, 900, size=(8, 8), dtype="uint16")
    red = rng.integers(40, 120, size=(8, 8), dtype="uint16")
    a = _write(tmp_path / "nir.tif", [nir])
    b = _write(tmp_path / "red.tif", [red])

    res = tools["spectral_index"](
        band_a=a, band_b=b, output_path=str(tmp_path / "s2_ndvi.tif"), kind="ndvi"
    )

    assert res["ok"] is True, res
    assert res["mean"] > 0.5


def test_detect_water_refuses_the_same_band_twice(tmp_path, tools):
    src = _rgb(tmp_path)
    mask = tmp_path / "water.tif"

    res = tools["detect_water"](green=src, nir=src, mask_path=str(mask))

    assert res["ok"] is False
    assert not mask.exists()
