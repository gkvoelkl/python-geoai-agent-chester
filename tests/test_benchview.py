"""Die Bench zeigt die Karte — auch die, die zu groß zum Einbetten ist.

Gemessen 2026-09-07 (`dop-aerial-regensburg`): `render_map` schrieb eine 12-MB-HTML
um ein 291-MB-Luftbild. Die Bench-UI hat sie mit „Too large to embed (12 MB)"
abgetan — obwohl `_write_picture_beside` daneben ein 677-KB-PNG gelegt hatte, genau
für diesen Fall („a channel that can show HTML shows it, one that cannot looks beside
it"). Wer prüfen soll, ob eine Karte stimmt, muss sie sehen; das ist dieselbe
Hausregel, die für den Agenten gilt.
"""

from __future__ import annotations

import contextlib

import pytest

import benchview


@pytest.fixture
def spy(monkeypatch):
    """Streamlit durch ein Protokoll ersetzen — geprüft wird, was gezeigt wurde."""
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
    """Der Fall aus dem Lauf: 12 MB, PNG daneben."""
    benchview.show_map(_html(tmp_path, 12, with_picture=True))
    assert not spy["iframe"], "12 MB werden nicht eingebettet"
    assert spy["image"] == [str(tmp_path / "karte.png")], "aber das Standbild schon"


def test_without_a_picture_the_size_is_named(tmp_path, spy):
    """Kein stilles Nichts: Wenn wirklich nichts zu zeigen ist, steht da, warum."""
    benchview.show_map(_html(tmp_path, 12, with_picture=False))
    assert not spy["iframe"] and not spy["image"]
    assert any("12 MB" in str(c) for c in spy["caption"])


def test_render_map_writes_the_picture_where_the_bench_looks(tmp_path):
    """Die Annahme hinter dem Rückfall, an ihrer Quelle geprüft."""
    from pathlib import Path

    from chester.capabilities import mapoutput

    src = Path(mapoutput.__file__).read_text(encoding="utf-8")
    assert 'Path(html_path).with_suffix(".png")' in src, (
        "benchview.show_map sucht das Standbild als Geschwisterdatei mit .png — "
        "ändert sich diese Konvention, findet die Bench nichts mehr")
