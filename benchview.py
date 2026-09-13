"""Die Ansichts-Helfer der Bench-UI — Karte, Raster, Artefakte eines Schrittes.

Aus `test_app.py` herausgelöst: dort sind sie zwischen Ablaufsteuerung und Layout
untergegangen, und die Datei stand an ihrer Baseline. Hier stehen sie beisammen, weil
sie eine gemeinsame Hausregel umsetzen — **dieselbe, die für den Agenten gilt**: Ein
Ergebnis wird angesehen, nicht behauptet. `ok: true` ist kein Beleg, „3 Datei(en)"
ist keine Karte, und „zu groß zum Einbetten" ist erst recht keine.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st


def live_sink(placeholder, *, tail: int = 6000):
    """Ein ``sink``, der den Strom laufend in ``placeholder`` zeichnet.

    Dasselbe Ein-Aufruf-Protokoll, das `ask.py` im Terminal bedient und
    ``stream_agent`` für die Chester-Zelle benutzt — hier herausgelöst, damit die
    Gegenprobe daneben nicht ihre eigene Variante bekommt. Zwei Ströme, die
    verglichen werden sollen, müssen gleich aussehen.

    ``tail`` deckelt, was gezeichnet wird: Streamlit rendert bei jedem Fragment neu,
    und ein Protokoll, das über die Laufzeit wächst, macht die Seite sonst zäh. Das
    vollständige Protokoll steht ohnehin im Live-Log auf der Platte.
    """
    chunks: list[str] = []

    def sink(chunk: str) -> None:
        chunks.append(chunk)
        placeholder.code("".join(chunks)[-tail:], language=None)

    return sink


def show_raster(path: str) -> None:
    """Ein GeoTIFF zeigen — samt der Zahlen, an denen man ein leeres erkennt."""
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
    """Eine gerenderte Karte zeigen — notfalls als Bild statt als Satz.

    Ein 12-MB-Luftbild-HTML wurde bis 2026-09-07 mit „zu groß zum Einbetten"
    abgetan, obwohl `render_map` daneben ein PNG schreibt. Wer prüfen soll, ob eine
    Karte stimmt, muss sie sehen.
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
    """Die Dateien eines Schrittes zeigen — Karte und Bild, nicht nur die Zahl.

    Ein Dialog, dessen Gegenstand eine Karte ist, wurde bis 2026-09-01 mit „3
    Datei(en)" zusammengefasst. Wer prüfen soll, ob eine Karte stimmt, muss sie sehen
    — dieselbe Hausregel, die für den Agenten gilt (`ok: true` ist kein Beleg), gilt
    für die Bench.
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
