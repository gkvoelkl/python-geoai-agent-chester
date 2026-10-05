"""The bench shows the map — including one too large to embed.

Measured 2026-09-07 (`dop-aerial-regensburg`): `render_map` wrote a 12 MB HTML around a
291 MB aerial image. The bench UI dismissed it with "Too large to embed (12 MB)" —
although `_write_picture_beside` had put a 677 KB PNG beside it, exactly for this case
("a channel that can show HTML shows it, one that cannot looks beside it"). Whoever is
to check whether a map is right must see it; that is the same house rule that applies
to the agent.
"""

from __future__ import annotations

import contextlib

import pytest

import benchview


@pytest.fixture
def spy(monkeypatch):
    """Replace Streamlit with a log — what is checked is what was shown."""
    seen: dict[str, list] = {"image": [], "iframe": [], "caption": []}

    @contextlib.contextmanager
    def expander(*_a, **_kw):
        yield

    monkeypatch.setattr(benchview.st, "expander", expander)
    for name in ("image", "iframe", "caption"):
        monkeypatch.setattr(benchview.st, name,
                            lambda *a, _n=name, **kw: seen[_n].append(a[0] if a else None))
    return seen


def _html(tmp_path, mb: float, with_picture: bool):
    page = tmp_path / "karte.html"
    page.write_text("<html>" + "x" * int(mb * 1_000_000) + "</html>", encoding="utf-8")
    if with_picture:
        (tmp_path / "karte.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(page)


def test_a_small_map_is_shown_interactively(tmp_path, spy):
    benchview.show_map(_html(tmp_path, 0.01, with_picture=True))
    assert len(spy["iframe"]) == 1 and not spy["image"]


def test_a_map_too_large_to_embed_falls_back_to_the_picture(tmp_path, spy):
    """The case from the run: 12 MB, PNG beside it."""
    benchview.show_map(_html(tmp_path, 12, with_picture=True))
    assert not spy["iframe"], "12 MB werden nicht eingebettet"
    assert spy["image"] == [str(tmp_path / "karte.png")], "aber das Standbild schon"


def test_without_a_picture_the_size_is_named(tmp_path, spy):
    """No silent nothing: when there really is nothing to show, it says why."""
    benchview.show_map(_html(tmp_path, 12, with_picture=False))
    assert not spy["iframe"] and not spy["image"]
    assert any("12 MB" in str(c) for c in spy["caption"])


def test_render_map_writes_the_picture_where_the_bench_looks(tmp_path):
    """The assumption behind the fallback, checked at its source."""
    from pathlib import Path

    from chester import mapguards  # `picture_beside` has lived here since Phase KM 1.5

    src = Path(mapguards.__file__).read_text(encoding="utf-8")
    assert 'Path(html_path).with_suffix(".png")' in src, (
        "benchview.show_map sucht das Standbild als Geschwisterdatei mit .png — "
        "ändert sich diese Konvention, findet die Bench nichts mehr")
