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
    _DEFAULT_REVIEW_PROMPT,
    _MIN_SPAN_DEG,
    _is_blank_image,
    _legend,
    _pad_extent,
    _review_prompt,
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


# ── the reviewer's prompt ────────────────────────────────────────────────────
# A snapshot is shapes on a backdrop; what they *mean* lives in the caller's
# head. On 2026-08-26 the reviewer twice judged the wrong thing: it read a blue
# city boundary as the vegetation layer, and on a Regensburg map it reported
# Munich street names — while the question asked about a buffer that had never
# been drawn. None of that is guesswork the image could have settled.


def test_legend_names_every_layer_with_its_colour():
    legend = _legend([
        {"layer": "forests.gpkg", "type": "vector", "features": 201,
         "geometry_types": ["Polygon"], "colour": "#3388ff"},
        {"layer": "roads.gpkg", "type": "vector", "features": 25674,
         "geometry_types": ["LineString"], "colour": "#e6550d"},
    ])
    assert "forests.gpkg: blue, 201 Polygon features" in legend
    assert "roads.gpkg: orange, 25674 LineString features" in legend
    assert "bottom to top" in legend  # draw order decides what hides what


def test_legend_describes_a_choropleth_by_its_column():
    legend = _legend([
        {"layer": "gem.gpkg", "type": "vector", "features": 12,
         "geometry_types": ["Polygon"], "colour": "shaded by einwohner with the YlOrRd colourmap"},
    ])
    assert "shaded by einwohner with the YlOrRd colourmap" in legend


def test_no_summary_leaves_the_question_untouched():
    """Nothing to say about the layers ⇒ no invented preamble."""
    assert _review_prompt("Does the buffer look right?", None) == "Does the buffer look right?"
    assert _review_prompt(None, []) == _DEFAULT_REVIEW_PROMPT


def test_the_prompt_forbids_judging_what_is_not_drawn():
    prompt = _review_prompt("Does the buffer look reasonable?", [
        {"layer": "forests.gpkg", "type": "vector", "features": 201,
         "geometry_types": ["Polygon"], "colour": "#3388ff"},
    ])
    assert "Does the buffer look reasonable?" in prompt  # the question survives
    assert "the image contains no others" in prompt
    assert "say so plainly" in prompt
    # The Munich rule: no place name that is not readable in the picture.
    assert "unless you can read that name as a label" in prompt
