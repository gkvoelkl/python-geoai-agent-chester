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
* **Keine Rahmenmaschinerie.** Chesters Agent führt zwei Werkzeuge, die aus *SelmaKit*
  stammen und hier nichts zu suchen haben: `write_plan` (Planung — genau die Führung,
  deren Beitrag F+ ↔ F+MCP misst) und `read_tool_result`. Das zweite hat eine Folge,
  die in die Messung gehört: Chester **kürzt** lange Werkzeugantworten und reicht ein
  Handle nach; über MCP kommt jede Antwort **ungekürzt** beim Client an und kostet
  dessen Kontext. Nachgezählt am 2026-09-14 (`use_qgis: false`): Agent 85 Werkzeuge,
  MCP 82 — dieselbe Menge minus `geo_python_run`, `inspect_map`, `write_plan`,
  `read_tool_result`, plus `validate_result`.
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
from typing import Any

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


def resolve_workspace(env: dict[str, str] | None = None) -> str:
    """Der Workspace des Servers — **absolut**, und unabhängig vom Startverzeichnis.

    `CHESTER_WORKSPACE` schlägt alles; sonst liegt der Workspace neben dem Paket, also
    dort, wo auch Chesters Agent ihn führt (ein gemeinsamer Cache, so entschieden).

    **Warum nicht einfach `DEFAULT_WORKSPACE`:** Der ist *relativ* (`.chester/workspace`)
    und hängt damit am Arbeitsverzeichnis des Prozesses. Chesters Agent wird aus dem
    Projektverzeichnis gestartet, ein MCP-Server nicht — Claude Desktop startet ihn mit
    einem Arbeitsverzeichnis, das niemand festgelegt hat. Gemessen 2026-09-14 mit
    ``cwd="/"``: Der Server stirbt beim Start an `'.chester/workspace'`. Laut immerhin,
    aber die Antwort auf „wohin schreibt er?" darf nicht „kommt drauf an, wie er
    gestartet wurde" lauten.

    Die Frage kann auch niemand sonst beantworten: **Der Client liefert keinen
    Workspace.** MCP kennt zwar `roots`, aber SEP-2577 hat server-initiierte Anfragen
    aus dem Protokoll entfernt — `ctx.list_roots()` gehört ausdrücklich nicht zur
    Server-API. Das Verzeichnis wird beim Start entschieden oder gar nicht.
    """
    source = os.environ if env is None else env
    gesetzt = source.get("CHESTER_WORKSPACE")
    if gesetzt:
        return str(Path(gesetzt).expanduser().resolve())
    return str((Path(__file__).resolve().parent.parent / DEFAULT_WORKSPACE).resolve())


#: Schalter für das **automatische** Anhängen des Standbilds an jede Rückgabe mit
#: `picture`. Vorgabe **aus**, und das ist eine Messentscheidung: In F+ sieht Chesters
#: Agent seine Karte auch nicht von selbst (die Sichtprüfung des Gates läuft erst ab
#: Stufe 2, Vorgabe ist 1; `inspect_map` muss er rufen). Automatisch angehängt bekäme
#: F+MCP einen Blick geschenkt, den F+ nicht hat — und die Messung wüsste nichts davon.
#: Für den Produktgebrauch anschalten; die Stellung gehört ins Laufprotokoll.
ATTACH_ENV = "CHESTER_MCP_ATTACH_PICTURES"

#: Ein Bild jenseits davon ist kein Bild mehr, sondern ein Unfall — und ein Unfall
#: gehört nicht in den Kontext eines fremden Clients.
MAX_BILD_BYTES = 5 * 1024 * 1024


def attach_pictures(env: dict[str, str] | None = None) -> bool:
    """Steht der Schalter für automatisch angehängte Standbilder auf an?"""
    source = os.environ if env is None else env
    return str(source.get(ATTACH_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


def _als_bild(rohdaten: str, media_type: str):
    """Base64 plus Medientyp → ein Bildblock des Protokolls."""
    from mcp.types import ImageContent

    return ImageContent(type="image", data=rohdaten, mime_type=media_type)


#: Datei, in der der Server jeden Aufruf mitschreibt — je Zeile ein JSON-Objekt.
CALL_LOG = "mcp-calls.jsonl"


def _protokolliere(workspace: str, name: str, dauer: float, ergebnis: Any) -> None:
    """Einen Werkzeugaufruf mitschreiben. Nie fatal — ein Protokoll kostet kein Ergebnis.

    **Warum der Server das selbst tun muss.** Claude Desktops MCP-Protokoll notiert
    `method="tools/call"` und lässt die Parameter weg — den Werkzeug*namen* nie
    (nachgesehen 2026-09-14). Von aussen ist damit nur die *Anzahl* der Aufrufe
    sichtbar, nicht welche. Für die Zelle F+MCP fehlte damit genau die Kennzahl, die
    die Bench für L+ und F+ mitschreibt: die Werkzeugabdeckung. Und Fragen wie „hat
    das Modell `validate_result` gerufen?" — die Kernfrage dieser Zelle — wären
    dauerhaft unbeantwortbar.

    Mitgeschrieben wird, **was** gerufen wurde und wie es ausging, nicht die Nutzlast:
    Argumente können Base64-Bilder oder ganze Geometrien enthalten, und ein Protokoll,
    das mitwächst, protokolliert bald nichts mehr.
    """
    import json
    import time

    try:
        zeile = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": name,
                 "duration_s": round(dauer, 3)}
        if isinstance(ergebnis, dict):
            zeile["ok"] = ergebnis.get("ok")
        with (Path(workspace) / CALL_LOG).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(zeile, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — ein Protokoll kostet nie ein Ergebnis
        pass


def read_call_log(workspace: str) -> list[dict]:
    """Die Aufrufe eines Laufs, älteste zuerst. Fehlende Datei → leere Liste."""
    import json

    pfad = Path(workspace) / CALL_LOG
    if not pfad.is_file():
        return []
    zeilen = []
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        if zeile.strip():
            try:
                zeilen.append(json.loads(zeile))
            except ValueError:
                continue
    return zeilen


def _mit_bild(tool: Callable[..., dict], *, automatisch: bool,
              workspace: str = "") -> Callable[..., Any]:
    """Bilder durch das Protokoll schicken — der eine Ort, an dem der Adapter mehr tut.

    Zwei Wege, und sie sind bewusst verschieden streng:

    * **Auf Anfrage.** Führt die Rückgabe ein ``content_base64`` samt ``media_type``
      (das tut nur `read_artifact`), wird sie **immer** als Bild geschickt: Der Client
      hat ausdrücklich danach gefragt. Der Base64-Klotz fliegt dabei aus der
      strukturierten Ausgabe — er reist im Bildblock, nicht zweimal.
    * **Automatisch.** Führt die Rückgabe ein ``picture`` (also einen Pfad), hängt das
      Bild nur an, wenn :data:`ATTACH_ENV` gesetzt ist. Vorgabe aus, siehe dort.

    Die strukturierte Ausgabe bleibt in beiden Fällen erhalten; nichts, was ein Client
    bisher lesen konnte, verschwindet.
    """
    import functools
    import time

    @functools.wraps(tool)
    def hülle(*args, **kwargs):
        start = time.monotonic()
        ergebnis = tool(*args, **kwargs)
        if workspace:
            _protokolliere(workspace, tool.__name__, time.monotonic() - start, ergebnis)
        if not isinstance(ergebnis, dict):
            return ergebnis
        try:
            from fastmcp.tools import ToolResult
            from fastmcp.utilities.types import Image

            roh = ergebnis.get("content_base64")
            if roh and str(ergebnis.get("media_type", "")).startswith("image/"):
                schlank = {k: v for k, v in ergebnis.items() if k != "content_base64"}
                return ToolResult(content=[_als_bild(roh, ergebnis["media_type"])],
                                  structured_content=schlank)

            bild = ergebnis.get("picture")
            if automatisch and bild and Path(bild).is_file() \
                    and Path(bild).stat().st_size <= MAX_BILD_BYTES:
                return ToolResult(content=[Image(path=str(bild)).to_image_content()],
                                  structured_content=ergebnis)
        except Exception:  # noqa: BLE001 — ein fehlendes Bild kostet nie das Ergebnis
            return ergebnis
        return ergebnis

    return hülle


def build_server(workspace: str = DEFAULT_WORKSPACE,
                 tools: list[Callable[..., dict]] | None = None):
    """Ein `FastMCP`-Server mit Chesters Geo-Werkzeugen, ohne Instruktionstext.

    ``tools`` nimmt eine bereits eingesammelte Liste entgegen, damit der Aufrufer sie
    nicht zweimal bauen muss (die Startmeldung nennt die Zahl).
    """
    from fastmcp import FastMCP

    server = FastMCP("chester")
    for tool in (collect_tools(workspace) if tools is None else tools):
        server.tool(_mit_bild(tool, automatisch=attach_pictures(),
                              workspace=workspace))
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
    workspace = resolve_workspace()
    try:
        Path(workspace).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"chester-mcp: Workspace {workspace} ist nicht anlegbar ({exc}). "
              "Setze CHESTER_WORKSPACE auf ein beschreibbares Verzeichnis.",
              file=sys.stderr)
        return 1
    tools = collect_tools(workspace)
    server = build_server(workspace, tools)
    print(f"chester-mcp: {len(tools)} Werkzeuge, Workspace {workspace}", file=sys.stderr)
    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover - Einstiegspunkt
    raise SystemExit(main())
