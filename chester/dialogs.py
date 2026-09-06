"""Die Prüfungen der Test-Level-4-Dialoge — rein, ohne Modell, ohne Netz.

Test-Level 4 prüft, was ein Einzelprompt prinzipiell nicht erreicht: Bezug,
Korrektur, Verfeinerung, veralteter Zustand, Standhalten, Herkunft, Reparatur auf
Zuruf (`doc/agent-test-dialogs.md`). Vieles davon braucht ein Urteil — aber
**nicht alles**, und was mechanisch prüfbar ist, gehört nicht vor einen Judge:

- „Er misst, bevor er erklärt" ist die Frage, ob in Schritt 2 ein Werkzeugaufruf das
  gemeldete Artefakt überhaupt angefasst hat.
- „Er nennt das kaputte Stück nicht erneut" ist eine Textprüfung.
- „Er geokodiert nicht neu" ist eine Werkzeugzählung.
- „Das neue Ergebnis ist nicht leer" ist `raster_degenerate` aus `geofacts`.

Was danach übrig bleibt — „benennt er die Ursache konkret" — ist echte Auslegung
und bleibt dem Menschen oder einem Judge überlassen; der Runner schreibt es
unbewertet ins Protokoll, statt ein Urteil zu erfinden.

Getrennt vom Runner, damit die Prüflogik ohne laufendes Modell testbar ist — dieselbe
Trennung wie `chester/probes.py` für Test-Level 2.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: „Regensburger Straße", „Regensburger Str.", „Regensburgerstraße" — ein Straßenname
#: hat im Deutschen mehrere gleich richtige Schreibweisen, und welche davon ankommt,
#: entscheiden OSM und das Modell, nicht der Testfall. Eine Prüfung auf die exakte
#: Zeichenkette misst deshalb die Schreibweise statt des Verhaltens: Sie fällt durch,
#: obwohl der Agent genau die richtige Straße geholt hat. Das ist derselbe Fehlertyp
#: wie ein Kriterium, das nach einem Ortswechsel stehenbleibt — ein garantiertes
#: Fehlurteil, das nichts über den Agenten sagt.
_STREET_SUFFIX = re.compile(r"str(asse)?\.?")
_NOISE = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    """Auf die Form bringen, in der zwei Schreibweisen desselben Namens gleich sind.

    Klein, ß→ss, jede Straßen-Endung auf ``str``, dann alles außer Buchstaben und
    Ziffern weg. Damit fallen Groß-/Kleinschreibung, Abkürzungspunkt, Getrennt- und
    Zusammenschreibung und der Bindestrich zusammen. Bewusst grob: Die Prüfung soll
    feststellen, *ob* die Straße angefasst wurde, nicht wie sie geschrieben stand.
    """
    lowered = text.casefold().replace("ß", "ss")
    return _NOISE.sub("", _STREET_SUFFIX.sub("str", lowered))

#: Alle unterstützten Prüfarten. Klein halten: Was Auslegung braucht, gehört nicht
#: hierher, sondern in die Prosa-Kriterien.
KINDS = (
    "tool_called",        # dieses Werkzeug lief in diesem Schritt
    "tool_not_called",    # dieses Werkzeug lief in diesem Schritt NICHT
    # irgendein Aufruf des Schrittes nennt diese Zeichenkette in seinen Argumenten
    "tool_touched",
    "answer_omits",       # die Antwort des Schrittes nennt diese Zeichenkette NICHT
    "no_dead_path",       # jeder Datei-Pfad in der Antwort existiert auch
    "no_flat_raster",     # kein in diesem Schritt erzeugtes Raster ist leer/einfarbig 0
    "fewer_calls_than",   # dieser Schritt kam mit weniger Aufrufen aus als jener
    # die zuletzt gezeichnete Karte trägt diese Geometrieart (point/line/polygon)
    "map_shows_family",
)


class Turn:
    """Was ein Schritt hinterlassen hat — die Eingabe jeder Prüfung.

    ``tool_calls`` ist die Liste ``(name, args)`` in Reihenfolge, ``tool_results``
    die Rückgaben, ``answer`` die **validierte** Endantwort (mit Gate-Notiz),
    ``written`` die Pfade, die dieser Schritt erzeugt hat.
    """

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        #: Der Schritt riss den Zeitdeckel — er hat **keine** Sitzung hinterlassen.
        self.timed_out = False
        self.tool_calls: list[tuple[str, Any]] = []
        self.tool_results: list[Any] = []
        self.answer: str = ""
        self.written: list[str] = []
        self.duration_s: float = 0.0

    @property
    def tools(self) -> list[str]:
        return [name for name, _args in self.tool_calls]

    def mentions_in_args(self, needle: str) -> bool:
        """Ob **irgendein** Aufruf dieses Schrittes den Begriff in den Argumenten nennt.

        Verglichen wird normalisiert (:func:`_normalize`), nicht wörtlich — sonst
        misst die Prüfung die Schreibweise statt des Verhaltens.
        """
        want = _normalize(needle)
        return any(want in _normalize(json.dumps(args, ensure_ascii=False, default=str))
                   for _name, args in self.tool_calls)


def aborted_after(turns: list[Turn]) -> int | None:
    """Nach welchem Schritt der Dialog abbrach — ``None``, wenn er durchlief.

    Ein Schritt, der den Zeitdeckel reißt, wird abgebrochen; SelmaKit schreibt die
    Sitzung nur bei vollständigem Lauf, also **hinterlässt er nichts**. Der nächste
    Schritt beginnt damit bei null — beobachtet am 2026-09-01, wo der Agent auf „Gib
    die Karte als GeoTiff aus" antwortete: *„Da dies unser erster Austausch ist …"*.

    Ab diesem Punkt ist kein Schritt mehr aussagekräftig: Er redet mit einem Fremden.
    Deshalb bricht der Runner ab, statt Zahlen zu erzeugen, die etwas anderes messen,
    als sie behaupten (vier von sieben Prüfungen bestanden damals, weil der zweite
    Schritt nichts tat — `fewer_calls_than: 0 gegen 28` war die deutlichste).
    """
    for i, t in enumerate(turns, 1):
        if t.timed_out:
            return i
    return None


def _turn(turns: list[Turn], index: Any) -> Turn | None:
    """Schritt 1 ist ``1``, nicht ``0`` — die Kriterien sprechen von Schritten, nicht Indizes."""
    try:
        i = int(index) - 1
    except (TypeError, ValueError):
        return None
    return turns[i] if 0 <= i < len(turns) else None


def check(assertion: dict, turns: list[Turn], *, workspace: Path) -> tuple[bool, str]:
    """Eine Prüfung auswerten → ``(bestanden, Begründung)``."""
    kind = assertion.get("kind")
    if kind not in KINDS:
        return False, f"unbekannte Prüfart {kind!r}"
    turn = _turn(turns, assertion.get("turn"))
    if turn is None:
        stopped = aborted_after(turns)
        if stopped is not None:
            return False, f"nicht gefahren — Dialog nach Schritt {stopped} abgebrochen"
        return False, f"Schritt {assertion.get('turn')!r} gibt es nicht"

    if kind == "tool_called":
        want = assertion["tool"]
        return want in turn.tools, f"{turn.tools.count(want)}× {want}"
    if kind == "tool_not_called":
        want = assertion["tool"]
        n = turn.tools.count(want)
        return n == 0, ("nicht aufgerufen" if n == 0 else f"{n}× {want}")
    if kind == "tool_touched":
        needle = assertion["contains"]
        hit = turn.mentions_in_args(needle)
        return hit, (f"{needle!r} kommt in den Argumenten vor" if hit
                     else f"kein Aufruf nennt {needle!r} (auch in keiner Schreibweise)"
                          " — gemessen wurde es nicht")
    if kind == "answer_omits":
        needle = assertion["contains"]
        hit = needle in turn.answer
        return not hit, ("nicht genannt" if not hit else f"{needle!r} steht in der Antwort")
    return _check_artifacts(kind, assertion, turn, turns, workspace)


def _check_artifacts(
    kind: str, assertion: dict, turn: Turn, turns: list[Turn], workspace: Path
) -> tuple[bool, str]:
    """Die Prüfarten, die über den Schritt hinaus auf Dateien oder andere Schritte sehen."""
    if kind == "fewer_calls_than":
        other = _turn(turns, assertion["than_turn"])
        if other is None:
            return False, f"Vergleichszug {assertion['than_turn']!r} gibt es nicht"
        return (len(turn.tool_calls) < len(other.tool_calls),
                f"{len(turn.tool_calls)} gegen {len(other.tool_calls)} Aufrufe")
    if kind == "no_dead_path":
        from chester.gate import _absent_claims

        dead = _absent_claims(turn.answer, str(workspace))
        return not dead, ("alle genannten Dateien existieren" if not dead
                          else f"nicht vorhanden: {', '.join(dead)}")
    if kind == "map_shows_family":
        return _map_family(assertion["family"], workspace)
    if kind == "no_flat_raster":
        from chester.geofacts import is_raster, raster_degenerate

        flat = [(Path(p).name, why) for p in turn.written
                if is_raster(p) and (why := raster_degenerate(p))]
        if not flat:
            return True, f"{len(turn.written)} Datei(en) erzeugt, kein leeres Raster"
        return False, "; ".join(f"{n}: {w}" for n, w in flat[:2])
    return False, "nicht ausgewertet"


def _map_family(family: str, workspace: Path) -> tuple[bool, str]:
    """Liegt auf der zuletzt gezeichneten Karte wirklich diese Geometrieart?

    Die Prüfung, die am 2026-09-01 gefehlt hat. Verlangt waren die **Grundflächen**
    von vier Adressen; gezeichnet wurden vier Punkte, weil `native:intersection` die
    Gebäude mit den geokodierten Punkten verschnitten hatte (Polygon ∩ Punkt = Punkt).
    Die Karte entstand, die Datei existierte, vier Objekte waren drin, die Spalten
    trugen `building=yes` — sieben von sieben Prüfungen grün, und auf dem Bild
    standen vier Kreise.

    Gefragt wird die **gezeichnete** Ebene (`last_map.json`), nicht irgendeine Datei
    des Schrittes: Der Lauf hatte die richtigen Polygone die ganze Zeit auf der
    Platte liegen — nur eben nicht auf der Karte.
    """
    from chester.geofacts import geometry_families

    # Beide Schreibweisen zulassen: `probe.workspace()` — was `dialog.py` und die
    # Test App durchreichen — liefert bereits das **geocache**-Verzeichnis, nicht die
    # Workspace-Wurzel. Diese Funktion hängte `geocache` ein zweites Mal an und suchte
    # in `…/geocache/geocache/`. Aufgefallen ist es erst am 2026-09-05, beim **ersten**
    # Lauf, in dem diese Prüfart überhaupt vorkam: `render_map` hatte zweimal `ok: true`
    # gemeldet, die Datei lag da, und die Prüfung meldete „keine Karte gezeichnet".
    # Eine Prüfung, die nie gelaufen ist, ist eine Behauptung.
    candidates = (workspace / "geocache" / "last_map.json", workspace / "last_map.json")
    record = next((c for c in candidates if c.is_file()), None)
    if record is None:
        return False, "keine Karte gezeichnet (kein last_map.json)"
    try:
        layers = json.loads(record.read_text(encoding="utf-8")).get("layers") or []
    except (OSError, ValueError):
        return False, "last_map.json ist nicht lesbar"
    found: set[str] = set()
    for path in layers:
        found |= geometry_families(path)
    if not found:
        return False, f"{len(layers)} Ebene(n) gezeichnet, keine mit lesbarer Geometrie"
    listed = ", ".join(sorted(found))
    return family in found, f"gezeichnet wurde: {listed}"


def evaluate(dialog: dict, turns: list[Turn], *, workspace: Path) -> tuple[bool, list[str]]:
    """Die maschinellen Prüfungen eines Dialogs → ``(bestanden, Protokollzeilen)``."""
    lines: list[str] = []
    passed = True
    stopped = aborted_after(turns)
    if stopped is not None:
        lines.append(
            f"  ✗ Dialog nach Schritt {stopped} abgebrochen: Der Schritt riss den "
            "Zeitdeckel und hinterließ keine Sitzung — jeder weitere Schritt begänne "
            "bei null und würde etwas anderes messen, als er behauptet."
        )
        passed = False
    for a in dialog.get("checks", []):
        ok, why = check(a, turns, workspace=workspace)
        passed &= ok
        lines.append(f"  {'✓' if ok else '✗'} Schritt {a.get('turn')} · {a['kind']}: {why}")
    return passed, lines


#: Wo die Dialogläufe liegen — eine Zeile je Dialog und Lauf.
HISTORY_PATH = Path(".chester") / "dialogs" / "history.jsonl"


def append_history(entry: dict, path: Path | None = None) -> None:
    """Ein Ergebnis anhängen. Best effort — ein Schreibfehler kostet keinen Lauf."""
    target = path or HISTORY_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_history(path: Path | None = None) -> list[dict]:
    """Die archivierten Dialogläufe, neueste zuletzt."""
    try:
        lines = (path or HISTORY_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


#: Welches Feld eine Prüfart neben ``kind`` und ``turn`` braucht. Aus `check()`
#: abgeleitet, nicht daneben gepflegt: Wer dort ein Feld liest, trägt es hier ein.
REQUIRED_ARGS = {
    "tool_called": "tool",
    "tool_not_called": "tool",
    "tool_touched": "contains",
    "answer_omits": "contains",
    "fewer_calls_than": "than_turn",
    "map_shows_family": "family",
    "no_dead_path": None,
    "no_flat_raster": None,
}

_FAMILIES = ("point", "line", "polygon")


def _assertion_problems(a: dict, j: int, n_turns: int) -> list[str]:
    """Was an einer einzelnen Zusicherung fehlt — die Fleißarbeit von `validate`."""
    kind = a.get("kind")
    if kind not in KINDS:
        return [f"Prüfung {j}: unbekannte Prüfart {kind!r} (erlaubt: {', '.join(KINDS)})"]
    out = []
    need = REQUIRED_ARGS.get(kind)
    if need and not str(a.get(need) or "").strip():
        out.append(f"Prüfung {j} ({kind}): Feld {need!r} fehlt")
    if kind == "map_shows_family" and a.get("family") not in _FAMILIES:
        out.append(f"Prüfung {j}: family muss {' / '.join(_FAMILIES)} sein")
    turn = a.get("turn")
    if not isinstance(turn, int) or not (1 <= turn <= n_turns):
        out.append(f"Prüfung {j}: turn={turn!r} — es gibt {n_turns} Schritt(e)")
    if kind == "fewer_calls_than":
        other = a.get("than_turn")
        if not isinstance(other, int) or not (1 <= other <= n_turns):
            out.append(f"Prüfung {j}: than_turn={other!r} zeigt auf keinen Schritt")
        elif other == turn:
            out.append(f"Prüfung {j}: vergleicht Schritt {turn} mit sich selbst")
    return out


def validate(dialog: dict) -> list[str]:
    """Alles, was an einem Dialogfall kaputt ist — leere Liste heißt in Ordnung.

    Gedacht für den Editor: Ein Fall mit unbekannter Prüfart oder fehlendem Feld
    scheitert sonst erst im Lauf, nach zwanzig Minuten Agentenzeit, mit einer
    Meldung über eine Zeile, die niemand geschrieben zu haben glaubt. Hier kostet
    derselbe Fehler eine rote Zeile im Formular.
    """
    problems: list[str] = []
    if not str(dialog.get("id") or "").strip():
        problems.append("id fehlt")
    turns = dialog.get("turns") or []
    if not turns:
        problems.append("kein einziger Schritt")
    for i, t in enumerate(turns, 1):
        if not str(t.get("prompt_de") or "").strip():
            problems.append(f"Schritt {i}: prompt_de ist leer")
    for j, a in enumerate(dialog.get("checks") or [], 1):
        problems.extend(_assertion_problems(a, j, len(turns)))
    return problems
