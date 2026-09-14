"""`read_artifact` — ein erzeugtes Artefakt auf Anfrage herausgeben.

Phase KM. Das Werkzeug gibt es, weil ein **Pfad für einen fremden Client keine
Referenz ist, sondern eine Zeichenkette**. Gemessen am 2026-09-14 (Zelle F+MCP,
Testfall 1): Das Modell erzeugte eine korrekte Karte, bekam ihren Pfad zurück, und
Claude Desktop meldete „Dateien, die an diesem Ort gespeichert sind, können nicht
angezeigt werden" — ersatzweise baute es aus den Zahlen ein Balkendiagramm. Der
Client darf Chesters Cache nicht lesen; sein eigener Ausgabepfad liegt in einer VM,
in die von aussen nichts zu mounten ist. Ein gemeinsames Dateisystem gibt es nicht
und kann es nicht geben — der Inhalt muss durch das Protokoll.

**Warum ein Werkzeug und nicht automatisch angehängt.** In F+ sieht Chesters Agent
seine Karte auch nicht von selbst: Die Sichtprüfung des Gates läuft erst ab
Strictness-Stufe 2, die Vorgabe ist 1, und `inspect_map` muss er rufen. Ein Werkzeug,
das gerufen werden *kann*, bildet dieselbe Lage ab. Hinge das Bild automatisch an
jeder Rückgabe, bekäme die Zelle F+MCP einen Blick geschenkt, den F+ nicht hat — und
die Messung wüsste nichts davon. (Für den Produktgebrauch gibt es den Schalter
`CHESTER_MCP_ATTACH_PICTURES`, siehe `chester/mcpserver.py`.)

**Eingesperrt, und das ist die halbe Konstruktion.** Ein Werkzeug, das Dateiinhalte
zurückgibt, ist ein Leseprimitiv. `chester.workspace.resolve_path` reicht beim Lesen
absolute Pfade absichtlich durch — Nutzerdaten am Ort zu lesen ist ein Merkmal, und
für Chesters eigenen Agenten harmlos, weil kein anderes Werkzeug Bytes zurückgibt.
Hier wäre es das nicht: `read_artifact("~/.ssh/id_rsa")` würde einem fremden Modell
den Schlüssel vorlegen. Deshalb löst dieses Modul **selbst** auf und verlangt, dass
das Ergebnis unter `<workspace>/geocache/` liegt.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

#: Bildformate, die als Bild durch das Protokoll reisen.
BILDER = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

#: Textformate, die als Text sinnvoll sind — mit Deckel, siehe unten.
TEXTE = {".csv": "text/csv", ".json": "application/json", ".txt": "text/plain",
         ".md": "text/markdown", ".geojson": "application/geo+json",
         ".wkt": "text/plain", ".prj": "text/plain"}

#: Ein Bild jenseits davon ist kein Bild mehr, sondern ein Unfall.
MAX_BILD_BYTES = 5 * 1024 * 1024

#: Text darüber hinaus wird gekürzt — mit Vermerk, nie stillschweigend.
MAX_TEXT_BYTES = 100 * 1024


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """`read_artifact`, an ``workspace`` gebunden."""
    ws = workspace

    def read_artifact(path: str) -> dict:
        """Give back the content of a file this server produced, so you can look at it.

        The paths other tools return are **strings, not references**: this server
        writes into its own cache, which your side cannot read. Call this to actually
        see what you made — most usefully on the ``picture`` that ``render_map``
        returns, which is a flat PNG of the same map.

        A PNG or JPEG comes back as an image. Small text files (CSV, JSON, GeoJSON,
        TXT) come back as text, truncated past 100 kB with a note saying so.

        Two things it refuses, each with the better route named: an HTML map (a web
        page with the data embedded — look at the ``.png`` beside it instead), and
        geodata such as GeoPackage, GeoTIFF or LAZ (use ``vector_info``,
        ``raster_info`` and the analysis tools; those files are data, not a view).

        Only files inside this server's own cache can be read. Anything else is
        refused — this is not a way to read the machine's filesystem.
        """
        cache = (Path(ws) / "geocache").resolve()
        ziel = (cache / Path(path).name).resolve()
        # Nur der Dateiname zählt: Ein Pfad von aussen darf nicht bestimmen, wo
        # gelesen wird. Das ist dieselbe Reduktion wie beim Schreiben.
        if not str(ziel).startswith(str(cache) + "/"):
            return {"ok": False, "error": "outside this server's cache"}
        if not ziel.is_file():
            nachbarn = sorted(p.name for p in cache.glob("*") if p.is_file())[:12]
            return {"ok": False, "error": f"no artifact named '{ziel.name}'",
                    "available": nachbarn}

        endung = ziel.suffix.lower()
        groesse = ziel.stat().st_size

        if endung in BILDER:
            if groesse > MAX_BILD_BYTES:
                return {"ok": False, "error": f"the picture is {groesse // 1024} kB, "
                        f"past the {MAX_BILD_BYTES // 1024} kB limit"}
            return {"ok": True, "path": str(ziel), "bytes": groesse,
                    "media_type": BILDER[endung],
                    "content_base64": base64.b64encode(ziel.read_bytes()).decode()}

        if endung == ".html":
            bild = ziel.with_suffix(".png")
            return {"ok": False,
                    "error": "an HTML map is a web page with the data embedded — "
                             "reading it as text tells you nothing about the picture",
                    "look_at_instead": str(bild) if bild.is_file() else None,
                    "hint": "call read_artifact on the .png beside it"}

        if endung in TEXTE:
            roh = ziel.read_bytes()[: MAX_TEXT_BYTES + 1]
            gekuerzt = len(roh) > MAX_TEXT_BYTES
            return {"ok": True, "path": str(ziel), "bytes": groesse,
                    "media_type": TEXTE[endung], "truncated": gekuerzt,
                    "text": roh[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")}

        return {"ok": False,
                "error": f"'{endung}' is geodata, not a view — handing back its bytes "
                         "would tell you nothing you can use",
                "use_instead": ["vector_info", "raster_info", "geocache_list"]}

    return [read_artifact]
