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

from chester.capabilities.mapoutput import _raster_to_rgba


def _rgb_with_nan_corner():
    data = np.random.default_rng(0).uniform(0, 255, (3, 16, 16)).astype("float32")
    data[:, :4, :4] = np.nan  # what reproject() leaves outside the source footprint
    return data


def test_rgb_composite_survives_nan_without_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)  # the cast warning must not fire
        rgba = _raster_to_rgba(_rgb_with_nan_corner(), None, "viridis")
    assert rgba.dtype == "uint8" and rgba.shape == (16, 16, 4)


def test_nan_pixels_are_transparent_and_valid_bytes():
    rgba = _raster_to_rgba(_rgb_with_nan_corner(), None, "viridis")
    assert (rgba[:4, :4, 3] == 0).all(), "NaN pixels must be fully transparent"
    # Alpha only hides them if the colour bytes are defined in the first place.
    assert rgba[:4, :4, :3].max() == 0
    assert (rgba[8:, 8:, 3] == 255).all(), "covered pixels must stay opaque"


def test_nodata_pixels_are_transparent():
    data = np.ones((3, 8, 8), dtype="float32") * 100
    data[:, 0, 0] = -9999
    rgba = _raster_to_rgba(data, -9999, "viridis")
    assert rgba[0, 0, 3] == 0
    assert (rgba[1:, 1:, 3] == 255).all()


def test_single_band_takes_the_colormap_path():
    data = np.linspace(0, 1, 64, dtype="float32").reshape(1, 8, 8)
    rgba = _raster_to_rgba(data, None, "viridis")
    assert rgba.shape == (8, 8, 4) and rgba.dtype == "uint8"
    assert (rgba[..., 3] == 255).all()
