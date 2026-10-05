"""The bench UI's view helpers — map, raster, the artifacts of a step.

Split out of `test_app.py`: there they got lost between flow control and layout, and
the file sat at its baseline. They stand together here because they implement one
house rule — **the same one that applies to the agent**: a result is looked at, not
claimed. `ok: true` is no evidence, "3 Datei(en)" is no map, and "too large to embed"
is even less of one.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st


def live_sink(placeholder, *, tail: int = 6000):
    """A ``sink`` that keeps drawing the stream into ``placeholder``.

    The same one-call protocol that `ask.py` serves in the terminal and ``stream_agent``
    uses for the Chester cell — split out here so the counter-run beside it does not get
    its own variant. Two streams meant to be compared must look alike.

    ``tail`` caps what gets drawn: Streamlit re-renders on every fragment, and a protocol
    that grows over the run makes the page sluggish otherwise. The full protocol is in
    the live log on disk anyway.
    """
    chunks: list[str] = []

    def sink(chunk: str) -> None:
        chunks.append(chunk)
        placeholder.code("".join(chunks)[-tail:], language=None)

    return sink


def show_raster(path: str) -> None:
    """Show a GeoTIFF — with the numbers that give an empty one away."""
    from chester.rasterview import preview

    made = preview(path)
    if made is None:
        st.caption(f"{Path(path).name}: nicht lesbar")
        return
    image, facts = made
    w, h = facts["size"]
    rng = facts.get("range")
    span = "nur nodata" if not rng else f"Werte {rng[0]:g}…{rng[1]:g}"
    if rng and rng[0] == rng[1]:
        span += " — ein einziger Wert, also eine einfarbige Fläche"
    st.image(image, width="stretch",
             caption=f"{Path(path).name} · {facts['bands']} Band(s) · {w}×{h} px · "
                     f"{facts['crs'] or 'ohne CRS'} · {span}")


def show_map(path: str, *, expanded: bool = True) -> None:
    """Show a rendered map — as an image if need be, rather than a sentence.

    Until 2026-09-07 a 12 MB aerial-image HTML was dismissed as "too large to embed",
    although `render_map` writes a PNG beside it. Whoever is to check whether a map is
    right must see it.
    """
    size = Path(path).stat().st_size
    with st.expander(f"Karte — {Path(path).name}", expanded=expanded):
        st.caption(f"`{path}`")
        if size < 8_000_000:
            st.iframe(Path(path).read_text(encoding="utf-8"), height=480)
            return
        picture = Path(path).with_suffix(".png")
        if picture.is_file():
            st.image(str(picture), width="stretch",
                     caption=f"{size // 1_000_000} MB interaktiv — hier als Standbild")
        else:
            st.caption(f"{size // 1_000_000} MB — zu groß zum Einbetten, kein PNG daneben")


def show_artifacts(paths: list[str], *, key: str) -> None:
    """Show a step's files — map and image, not just the count.

    Until 2026-09-01 a dialogue whose subject is a map was summed up as "3 Datei(en)".
    Whoever is to check whether a map is right must see it — the same house rule that
    applies to the agent (`ok: true` is no evidence) applies to the bench.
    """
    if not paths:
        st.caption("keine Datei erzeugt")
        return
    shown = [p for p in paths if not p.endswith(".meta.json")]
    st.caption(" · ".join(Path(p).name for p in shown) or "nur Sidecars")
    for path in shown:
        suffix = Path(path).suffix.lower()
        if suffix == ".png":
            st.image(path, caption=Path(path).name, width="stretch")
        elif suffix in (".tif", ".tiff"):
            show_raster(path)
        elif suffix == ".html":
            show_map(path)
