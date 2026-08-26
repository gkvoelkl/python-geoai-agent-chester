"""Tests for the raster→RGBA conversion behind `render_map`'s image overlay.

The load-bearing case is the **reprojected multi-band** raster: warping fills the
uncovered corners with NaN, and the RGB composite path casts to uint8 — so a
non-finite pixel that reaches the cast is undefined behaviour (it warns and yields
garbage). Surfaced by the DOP connector, the first heavy 3-band raster Chester
renders; before that only single-band DEM/index rasters exercised this code, and
those take the colormap path where matplotlib absorbs the NaN.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from chester.capabilities.mapoutput import _raster_to_rgba


def _rgb_with_nan_corner():
    data = np.random.default_rng(0).uniform(0, 255, (3, 16, 16)).astype("float32")
    data[:, :4, :4] = np.nan  # what reproject() leaves outside the source footprint
    return data


def test_rgb_composite_survives_nan_without_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)  # the cast warning must not fire
        rgba, _ = _raster_to_rgba(_rgb_with_nan_corner(), None, "viridis")
    assert rgba.dtype == "uint8" and rgba.shape == (16, 16, 4)


def test_nan_pixels_are_transparent_and_valid_bytes():
    rgba, _ = _raster_to_rgba(_rgb_with_nan_corner(), None, "viridis")
    assert (rgba[:4, :4, 3] == 0).all(), "NaN pixels must be fully transparent"
    # Alpha only hides them if the colour bytes are defined in the first place.
    assert rgba[:4, :4, :3].max() == 0
    assert (rgba[8:, 8:, 3] == 255).all(), "covered pixels must stay opaque"


def test_nodata_pixels_are_transparent():
    data = np.ones((3, 8, 8), dtype="float32") * 100
    data[:, 0, 0] = -9999
    rgba, _ = _raster_to_rgba(data, -9999, "viridis")
    assert rgba[0, 0, 3] == 0
    assert (rgba[1:, 1:, 3] == 255).all()


def test_single_band_takes_the_colormap_path():
    data = np.linspace(0, 1, 64, dtype="float32").reshape(1, 8, 8)
    rgba, scale = _raster_to_rgba(data, None, "viridis")
    assert rgba.shape == (8, 8, 4) and rgba.dtype == "uint8"
    assert (rgba[..., 3] == 255).all()
    # The ramp reports the values it spans — a picture nobody can calibrate is
    # read by guessing, and the guess comes back worded like a measurement.
    assert scale["vmin"] < scale["vmax"]


def test_an_rgb_composite_reports_no_scale():
    """Three bands are a photograph, not a measurement — labelling it would lie."""
    rgba, scale = _raster_to_rgba(_rgb_with_nan_corner(), None, "viridis")
    assert rgba.shape == (16, 16, 4)
    assert scale is None


def test_a_colourised_raster_snapshot_carries_a_labelled_scale(tmp_path):
    """Regression: the visual check must say which end of the ramp is high.

    From the `dop-ndvi-no-nir-bayern` run — `inspect_map` drew an NDVI with the
    default `YlOrRd` and no legend, and the vision model read the ramp backwards:
    it called the pale Danube "vegetation" and the dark parks "non-vegetated".
    Pale is `YlOrRd`'s *low* end. Nothing in the image said so, so the verdict was
    a coin flip returned as a confident yes.
    """
    import rasterio
    from rasterio.transform import from_origin

    from chester.capabilities.mapoutput import _render_snapshot

    path = tmp_path / "ndvi.tif"
    band = np.linspace(-0.9, 0.99, 64, dtype="float32").reshape(8, 8)
    with rasterio.open(
        path, "w", driver="GTiff", height=8, width=8, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_origin(12.08, 49.02, 0.001, 0.001),
    ) as dst:
        dst.write(band, 1)

    png, summary = _render_snapshot(
        [str(path)], str(tmp_path), None, "quantiles", 5, "YlOrRd", "check"
    )
    assert png[:4] == b"\x89PNG"
    # The facts name the span, so a reader who cannot see colour still has it.
    lo, hi = summary[0]["value_range"]
    assert lo < hi and lo < 0 < hi  # this band runs -0.9 .. 0.99


def test_the_colour_bar_is_drawn_and_spans_the_data():
    """The bar itself: a second axes whose scale matches the values it explains."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from chester.capabilities.mapoutput import _add_colourbar

    fig, ax = plt.subplots()
    assert len(fig.axes) == 1
    _add_colourbar(fig, ax, "YlOrRd", {"vmin": -0.9, "vmax": 0.99}, "ndvi.tif")
    assert len(fig.axes) == 2, "no colour bar axes was added"
    bar_ax = fig.axes[1]
    assert bar_ax.get_xlim() == pytest.approx((-0.9, 0.99))
    plt.close(fig)
