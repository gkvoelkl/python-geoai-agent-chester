"""The validation snapshot's framing and backdrop guards (capabilities.mapoutput).

These are the two silent failures found on 2026-08-16 by *looking at* the rendered
PNGs rather than at return values — both made the level-2 visual check blind to the
error class it exists for (a CRS bug that moves data into the sea):

* the aerial WMS answers outside its coverage with a blank white image, which the
  code accepted as a backdrop and so never fell back to OSM;
* a single-feature layer has a zero-width bounding box, so there was no map area
  around it at all.

No network here — the blankness rule and the framing rule are both pure.
"""

from __future__ import annotations

import io

import pytest

matplotlib = pytest.importorskip("matplotlib")
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  - must follow the Agg backend choice
from PIL import Image  # noqa: E402

from chester.capabilities.mapoutput import (  # noqa: E402
    _MIN_SPAN_DEG,
    _is_blank_image,
    _pad_extent,
)


def _png(color, size=(40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _noisy_png(size=(40, 30)) -> bytes:
    img = Image.new("RGB", size)
    img.putdata([(x * 6 % 256, y * 8 % 256, 90) for y in range(size[1]) for x in range(size[0])])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_uniform_white_counts_as_blank():
    # The measured real case: WMS outside its coverage, HTTP 200, std 0.00.
    assert _is_blank_image(_png((255, 255, 255))) is True


def test_uniform_dark_counts_as_blank_too():
    # Not every "no data" answer is white; uniformity is the signal, not the colour.
    assert _is_blank_image(_png((12, 12, 12))) is True


def test_real_imagery_is_not_blank():
    assert _is_blank_image(_noisy_png()) is False


def test_undecodable_bytes_count_as_blank():
    # A picture we cannot inspect must not be trusted as a backdrop.
    assert _is_blank_image(b"not a png") is True


def _extent(ax):
    west, east = ax.get_xlim()
    south, north = ax.get_ylim()
    return east - west, north - south


def test_zero_extent_gets_a_real_frame():
    fig, ax = plt.subplots()
    ax.set_xlim(12.1, 12.1)  # a single point: both spans collapse
    ax.set_ylim(49.0, 49.0)
    _pad_extent(ax)
    span_x, span_y = _extent(ax)
    assert span_x >= _MIN_SPAN_DEG and span_y >= _MIN_SPAN_DEG
    plt.close(fig)


def test_frame_stays_centred_on_the_data():
    fig, ax = plt.subplots()
    ax.set_xlim(12.1, 12.1)
    ax.set_ylim(49.0, 49.0)
    _pad_extent(ax)
    west, east = ax.get_xlim()
    south, north = ax.get_ylim()
    assert (west + east) / 2 == pytest.approx(12.1)
    assert (south + north) / 2 == pytest.approx(49.0)
    plt.close(fig)


def test_sliver_extent_is_widened_to_a_readable_ratio():
    # A long thin layer otherwise renders as a strip nothing is recognisable in.
    fig, ax = plt.subplots()
    ax.set_xlim(12.0, 12.01)
    ax.set_ylim(48.0, 49.0)
    _pad_extent(ax)
    span_x, span_y = _extent(ax)
    # The cap is exactly 2:1, so this lands on the boundary: compare the ratio with a
    # tolerance rather than loosening the rule the code enforces.
    assert span_y / span_x <= 2 + 1e-9
    plt.close(fig)


def test_normal_extent_only_gains_a_margin():
    fig, ax = plt.subplots()
    ax.set_xlim(12.0, 12.2)
    ax.set_ylim(48.9, 49.1)
    _pad_extent(ax)
    span_x, span_y = _extent(ax)
    # Padded, but not blown up: the data still fills most of the frame.
    assert 0.2 < span_x < 0.3
    assert 0.2 < span_y < 0.3
    plt.close(fig)
