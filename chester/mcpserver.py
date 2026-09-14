"""Chester-MCP — Chesters Geo-Werkzeuge für einen fremden Client, über stdio.

Phase KM, Schritt 3. Ein **Kanal** neben Webchat und Telegram, kein Fork: Der Server
sammelt die Hüllenschicht (`chester/*tools.py`) ein und meldet sie an. Es gibt keine
zweite Werkzeugdefinition — das ist der ganze Zweck der Schicht.

**Was hier bewusst fehlt.**

* **Kein Instruktionstext.** Weder Vorspann noch Regelblock. Was im fremden Harness
  wirkt, ist nicht, was ein Werkzeug *sagt*, sondern was es *tut* und *zurückmeldet*:
  `osm_features` schneidet bei einem benannten Ort auf die amtliche Grenze und meldet
  `clipped_to_place`; metrische Arbeit in einem geographischen CRS wird abgelehnt, nicht
  abgeraten; ein vertippter Ebenenname bekommt `did_you_mean`. Eine Warnung im
  Rückgabewert ist eine Tatsache über die Welt, kein Befehl — sie wirkt auch bei einem
  Modell, das Anweisungen aus Werkzeugtexten ignoriert (`internal/chester-mcp.md` §4b).
  Werkzeug-lokale Dokumentation steckt im **Docstring**; für einen MCP-Client ist er der
  einzige Textkanal, der das Modell nachweislich erreicht (gemessen 2026-09-13).
* **Kein `geo_python_run`, kein `qgis_python`.** Der Notausgang bleibt Chesters eigenem
  Agenten vorbehalten; damit ist der Server frei von Fernausführung. Preis, benannt: Was
  kein Werkzeug abdeckt, ist über MCP nicht erreichbar.
* **Kein Zwang.** `validate_result` liefert dieselben Befunde wie das Gate, aber nichts
  hält einen fremden Client an, es zu rufen. Genau dieser Wegfall ist der Messgegenstand
  der Zelle F+MCP — er steht als `enforced: false` in jedem Rückgabewert.

Nur **lokal, nur stdio**: ein Client je Prozess, ein gemeinsamer Workspace wie heute.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from pathlib import Path

from chester.workspace import DEFAULT_WORKSPACE

#: Hüllenmodule, die **nicht** ausgeliefert werden — und das ist absichtlich leer.
#: Entschieden 2026-09-13: **alle ausliefern**, kein kuratierter Teilsatz. Die vier
#: Ausnahmen (`geo_python_run`, `qgis_python`, `inspect_map`, skillguide/runlog) liegen
#: ohnehin ausserhalb der Hüllenschicht — sie sind an den Rahmen gebunden, nicht
#: ausgeschlossen. Die Liste bleibt als benannter Ort, falls je eine dazukommt.
EXCLUDED: frozenset[str] = frozenset()


def wrapper_modules() -> list[str]:
    """Die Namen der Hüllenmodule, in Katalogreihenfolge — ohne die ausgeschlossenen."""
    here = Path(__file__).parent
    return sorted(
        p.stem for p in here.glob("*tools.py") if p.stem not in EXCLUDED
    )


def collect_tools(workspace: str) -> list[Callable[..., dict]]:
    """Jedes Werkzeug der Hüllenschicht, an ``workspace`` gebunden.

    Ein Modul ohne `build_tools` ist ein **Fehler**, keine Auslassung: Es still zu
    überspringen hiesse, einen kleineren Katalog auszuliefern, ohne dass es jemand
    merkt — genau das passierte `vectoroptools`, solange es `op_tools` hiess
    (2026-09-14). `tests/test_structure.py` prüft denselben Vertrag.
    """
    tools: list[Callable[..., dict]] = []
    seen: dict[str, str] = {}
    for name in wrapper_modules():
        module = importlib.import_module(f"chester.{name}")
        build = getattr(module, "build_tools", None)
        if build is None:
            raise RuntimeError(
                f"chester.{name} exportiert kein `build_tools` — "
                "die Hüllenschicht hat genau einen Einstiegspunkt."
            )
        for tool in build(workspace):
            if tool.__name__ in seen:
                raise RuntimeError(
                    f"Werkzeugname doppelt: {tool.__name__} "
                    f"({seen[tool.__name__]} und {name})"
                )
            seen[tool.__name__] = name
            tools.append(tool)
    return tools


def build_server(workspace: str = DEFAULT_WORKSPACE):
    """Ein `FastMCP`-Server mit Chesters Geo-Werkzeugen, ohne Instruktionstext."""
    from fastmcp import FastMCP

    server = FastMCP("chester")
    for tool in collect_tools(workspace):
        server.tool(tool)
    return server


def main(argv: list[str] | None = None) -> int:
    """Einstiegspunkt — stdio, bis der Client den Kanal schliesst.

    Gerufen als ``uv run python -m chester.mcpserver``. **Kein `[project.scripts]`:**
    Dieses Projekt hat bewusst kein `build-system`, uv führt es als virtuelles
    Projekt — ein Skript-Eintrag würde nie installiert und sähe nur so aus, als
    gäbe es den Befehl. `uv run` ist ohnehin die Hausform (`ask.py`, `probe.py`).

    Der Workspace kommt aus `CHESTER_WORKSPACE` oder ist der übliche; er wird
    angelegt, falls er fehlt. **Kein Modell, kein Anbieter, keine `chester.json`** —
    wer nur den Server will, braucht die Einrichtungszeremonie des Agenten nicht.

    Meldungen gehen auf **stderr**: stdout ist der Protokollkanal, jedes Zeichen
    darauf zerstört die Sitzung.
    """
    del argv
    workspace = os.environ.get("CHESTER_WORKSPACE") or DEFAULT_WORKSPACE
    Path(workspace).mkdir(parents=True, exist_ok=True)
    server = build_server(workspace)
    print(f"chester-mcp: {len(collect_tools(workspace))} Werkzeuge, "
          f"Workspace {workspace}", file=sys.stderr)
    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover - Einstiegspunkt
    raise SystemExit(main())
