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
