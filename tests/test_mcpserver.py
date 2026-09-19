"""Test-Level 2 für Chester-MCP — der Katalog, den ein fremder Client sieht.

Geprüft wird der **Katalog und der Vertrag**, nicht die Werkzeuge selbst: Die stehen
in der Hüllenschicht und haben dort ihre eigenen Prüfungen. Hier geht es um das, was
nur beim Ausliefern schiefgehen kann — ein fehlendes Werkzeug, ein zu viel
ausgeliefertes, eine Beschreibung, die beim Client leer ankommt.

Ohne Netz, ohne Modell, ohne laufenden Server: `collect_tools` ist reine Funktion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chester import mcpserver

WS = "/tmp/chester-mcp-test"


def _names() -> set[str]:
    return {t.__name__ for t in mcpserver.collect_tools(WS)}


def test_the_catalogue_carries_the_whole_wrapper_layer():
    """Jedes Hüllenmodul kommt mit — ohne Kuratierung.

    Entschieden 2026-09-13: alle ausliefern. Ob ein fremdes Modell bei dieser Breite
    noch das richtige Werkzeug wählt, ist selbst ein Befund der Zelle F+MCP und darf
    nicht vorab durch eine engere Auswahl weggeräumt werden.
    """
    modules = sorted(p.stem for p in (Path(mcpserver.__file__).parent).glob("*tools.py"))
    assert mcpserver.wrapper_modules() == modules, "ein Hüllenmodul fehlt im Katalog"
    assert len(_names()) >= 80, "der Katalog ist unerwartet klein"


def test_the_escape_hatches_stay_out_of_the_catalogue():
    """Kein `geo_python_run`, kein `qgis_python`, kein `inspect_map`.

    Entschieden 2026-09-13: Der Notausgang bleibt Chesters eigenem Agenten. Damit ist
    der Server frei von Fernausführung — und die Frage stellt sich später nicht neu.
    Sie liegen ausserhalb der Hüllenschicht; dieser Test hält fest, dass sie nicht
    eines Tages hineinwandern.
    """
    verboten = {"geo_python_run", "qgis_python", "inspect_map"} & _names()
    assert not verboten, f"Fernausführung im MCP-Katalog: {sorted(verboten)}"


def test_every_tool_arrives_with_a_description():
    """Der Docstring ist für einen MCP-Client der einzige Textkanal zum Modell.

    Gemessen 2026-09-13: Server-`instructions` erreichen den Nutzer über die
    `remote-devices`-Brücke nicht, Werkzeugbeschreibungen wörtlich schon. Ein
    Werkzeug ohne Docstring ist dort also stumm.
    """
    ohne = [t.__name__ for t in mcpserver.collect_tools(WS)
            if not (t.__doc__ or "").strip()]
    assert not ohne, f"stumme Werkzeuge: {ohne}"


def test_the_gate_is_offered_and_says_it_does_not_enforce():
    """`validate_result` ist da — und sagt selbst, dass es nichts erzwingt.

    Das ist der Messgegenstand der Zelle, kein Umsetzungsmangel: Über MCP wird aus
    „Correctness is a loop phase" ein Werkzeug, das gerufen werden *kann*. Wäre der
    Rückgabewert darüber stumm, läse sich ein nicht gerufenes Gate wie ein bestandenes.
    """
    tools = {t.__name__: t for t in mcpserver.collect_tools(WS)}
    assert "validate_result" in tools
    result = tools["validate_result"](["gibtsnicht.gpkg"])
    assert result["enforced"] is False
    assert result["must_fix"] is True          # eine fehlende Datei ist ein Befund
    assert result["findings"][0]["check"] == "exists"


def test_a_wrapper_module_without_build_tools_is_an_error(monkeypatch):
    """Still einen kleineren Katalog auszuliefern ist das eigentliche Risiko.

    Genau das geschah, solange `vectoroptools` `op_tools` hiess: kein Fehler, nur
    zehn fehlende Vektoroperationen (2026-09-14). Der Server bricht deshalb ab,
    statt zu überspringen.
    """
    monkeypatch.setattr(mcpserver, "wrapper_modules", lambda: ["workspace"])
    with pytest.raises(RuntimeError, match="build_tools"):
        mcpserver.collect_tools(WS)


def test_the_server_really_speaks_the_protocol(tmp_path):
    """Als **eigener Prozess** über stdio — alles andere prüft nur den Katalog.

    Die Tests darüber rufen `collect_tools` im selben Prozess; sie blieben auch dann
    grün, wenn der Server gar nicht startet oder wenn `FastMCP` eines der Werkzeuge
    nicht annimmt. Hier läuft er so, wie er beim Nutzer läuft: eigener Prozess,
    Handshake, Katalogabfrage, echter Aufruf.

    **Was er nicht abdeckt, gegengeprüft am 2026-09-14:** eine versehentliche Zeile
    auf stdout. Eingebaut und erwartet, dass der Test fällt — er blieb grün. Weder
    `fastmcp` noch das `mcp`-Paket leiten stdout um; die Zeile ging vor dem Handshake
    hinaus und der Client übersprang sie stillschweigend. Eine Zeile *während* der
    Sitzung ist damit nicht entlastet — sie ist nur ungeprüft.
    """
    import asyncio
    import os
    import sys

    fastmcp = pytest.importorskip("fastmcp")
    from fastmcp.client.transports import StdioTransport

    async def frage() -> tuple[int, dict]:
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "chester.mcpserver"],
            env={**os.environ, "CHESTER_WORKSPACE": str(tmp_path)},
        )
        async with fastmcp.Client(transport) as client:
            tools = await client.list_tools()
            antwort = await client.call_tool("validate_result", {"paths": ["fehlt.gpkg"]})
            return len(tools), antwort.data

    anzahl, befund = asyncio.run(frage())
    assert anzahl >= 80, "der Server meldet einen zu kleinen Katalog an"
    assert befund["must_fix"] is True and befund["enforced"] is False


def test_the_workspace_does_not_depend_on_the_working_directory(monkeypatch, tmp_path):
    """Wohin der Server schreibt, darf nicht davon abhängen, wie er gestartet wurde.

    `DEFAULT_WORKSPACE` ist relativ (`.chester/workspace`). Chesters Agent läuft aus
    dem Projektverzeichnis, ein MCP-Server nicht: Claude Desktop startet ihn mit einem
    Arbeitsverzeichnis, das niemand festgelegt hat. Gemessen 2026-09-14 mit ``cwd="/"``
    starb der Server an `'.chester/workspace'`.

    Und nachfragen kann er auch nicht: **Der Client liefert keinen Workspace.** MCP
    kennt `roots`, aber SEP-2577 hat server-initiierte Anfragen aus dem Protokoll
    entfernt. Das Verzeichnis wird beim Start entschieden oder gar nicht.
    """
    import os

    monkeypatch.chdir(tmp_path)
    ohne_env = mcpserver.resolve_workspace({})
    assert os.path.isabs(ohne_env)
    assert str(tmp_path) not in ohne_env, "der Workspace folgt dem Arbeitsverzeichnis"

    ziel = tmp_path / "eigener"
    assert mcpserver.resolve_workspace({"CHESTER_WORKSPACE": str(ziel)}) == str(ziel)


def test_read_artifact_cannot_leave_the_cache(tmp_path):
    """Ein Werkzeug, das Bytes zurückgibt, ist ein Leseprimitiv — und muss eingesperrt sein.

    `chester.workspace.resolve_path` reicht beim **Lesen** absolute Pfade absichtlich
    durch: Nutzerdaten am Ort zu lesen ist ein Merkmal, und für Chesters eigenen
    Agenten harmlos, weil kein anderes Werkzeug Dateiinhalte herausgibt. Für
    `read_artifact` gilt das nicht — es löst deshalb selbst auf und reduziert auf den
    Dateinamen.
    """
    from chester.artifacttools import build_tools

    (tmp_path / "geocache").mkdir()
    geheim = tmp_path.parent / "geheim.txt"
    geheim.write_text("nicht für fremde Augen")

    lies = build_tools(str(tmp_path))[0]
    for roh in (str(geheim), "/etc/passwd", "../geheim.txt", "~/.ssh/id_rsa"):
        r = lies(roh)
        assert r["ok"] is False, f"{roh!r} kam durch"
        assert "nicht für fremde Augen" not in str(r)


def test_read_artifact_gives_pictures_and_refuses_data(tmp_path):
    """Bild als Bild, Text als Text, Geodaten mit begründeter Absage.

    Die Absagen nennen den besseren Weg, statt nur nein zu sagen: eine HTML-Karte ist
    eine Webseite mit eingebetteten Daten — als Text sagt sie nichts über das Bild —,
    und ein GeoPackage ist kein Anblick, sondern ein Datensatz für `vector_info`.
    """
    from chester.artifacttools import build_tools

    cache = tmp_path / "geocache"
    cache.mkdir()
    (cache / "k.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (cache / "k.html").write_text("<html>viel eingebettetes GeoJSON</html>")
    (cache / "d.gpkg").write_bytes(b"SQLite format 3\x00")
    (cache / "t.csv").write_text("a,b\n1,2\n")

    lies = build_tools(str(tmp_path))[0]

    bild = lies("k.png")
    assert bild["ok"] and bild["media_type"] == "image/png" and bild["content_base64"]

    text = lies("t.csv")
    assert text["ok"] and text["text"].startswith("a,b") and text["truncated"] is False

    html = lies("k.html")
    assert html["ok"] is False and html["look_at_instead"].endswith("k.png")

    daten = lies("d.gpkg")
    assert daten["ok"] is False and "vector_info" in daten["use_instead"]


def test_the_picture_is_attached_only_on_request_by_default():
    """Vorgabe aus — sonst bekäme F+MCP einen Blick, den F+ nicht hat.

    In F+ sieht Chesters Agent seine Karte nicht von selbst: Die Sichtprüfung des
    Gates läuft erst ab Stufe 2, die Vorgabe ist 1, und `inspect_map` muss er rufen.
    Ein automatisch angehängtes Bild wäre also kein Gleichstand, sondern ein Vorsprung
    — und einer, von dem die Auswertung nichts wüsste. Auf Anfrage (`read_artifact`,
    erkennbar an `content_base64`) reist das Bild dagegen **immer**.
    """
    assert mcpserver.attach_pictures({}) is False
    assert mcpserver.attach_pictures({mcpserver.ATTACH_ENV: "1"}) is True

    def mit_pfad() -> dict:
        """Eine Karte mit Standbild-Pfad."""
        return {"ok": True, "picture": "/gibt/es/nicht.png"}

    def auf_anfrage() -> dict:
        """Ein ausdrücklich angefordertes Bild."""
        return {"ok": True, "media_type": "image/png", "content_base64": "AAAA"}

    assert mcpserver._with_picture(mit_pfad, automatic=False)() == {
        "ok": True, "picture": "/gibt/es/nicht.png"}

    geliefert = mcpserver._with_picture(auf_anfrage, automatic=False)()
    assert [type(b).__name__ for b in geliefert.content] == ["ImageContent"]
    # Der Base64-Klotz reist im Bildblock, nicht zusätzlich in der Struktur.
    assert "content_base64" not in geliefert.structured_content


def test_the_server_records_which_tools_were_called(tmp_path):
    """Ohne eigenes Protokoll ist die Zelle F+MCP nicht auswertbar.

    Claude Desktops MCP-Protokoll notiert `method="tools/call"` und lässt die
    Parameter weg — den Werkzeug*namen* nie (nachgesehen 2026-09-14). Von aussen ist
    damit nur die Anzahl der Aufrufe sichtbar. Für L+ und F+ schreibt die Bench
    Werkzeugzahl, verschiedene Werkzeuge und Abdeckung mit; ohne diese Datei hätte
    F+MCP davon nichts — und die Kernfrage der Zelle, ob das Modell das freiwillige
    `validate_result` ruft, bliebe dauerhaft unbeantwortbar.

    Mitgeschrieben wird **was** und **wie es ausging**, nicht die Nutzlast: Argumente
    können Base64-Bilder oder ganze Geometrien tragen.
    """
    def geht_gut() -> dict:
        """Ein Werkzeug."""
        return {"ok": True, "features": 3}

    def geht_schief() -> dict:
        """Noch eins."""
        return {"ok": False, "error": "nein"}

    for fn in (geht_gut, geht_schief, geht_gut):
        mcpserver._with_picture(fn, automatic=False, workspace=str(tmp_path))()

    zeilen = mcpserver.read_call_log(str(tmp_path))
    assert [z["tool"] for z in zeilen] == ["geht_gut", "geht_schief", "geht_gut"]
    assert [z["ok"] for z in zeilen] == [True, False, True]
    assert all("duration_s" in z and "ts" in z for z in zeilen)
    # Keine Nutzlast im Protokoll.
    assert all(set(z) <= {"ts", "tool", "duration_s", "ok"} for z in zeilen)

    # Ohne Workspace wird nicht protokolliert (Aufrufe im Test, Adapter ohne Ziel).
    assert mcpserver.read_call_log(str(tmp_path / "leer")) == []
