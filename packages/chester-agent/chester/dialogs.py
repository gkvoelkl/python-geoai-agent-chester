"""The checks of the Test-Level-4 dialogues — pure, no model, no network.

Test-Level 4 checks what a single prompt cannot reach in principle: reference,
correction, refinement, stale state, holding firm, provenance, repair on request
(`doc/agent-test-dialogs.md`). Much of that needs judgement — but **not all of it**,
and whatever can be checked mechanically does not belong in front of a judge:

- "It measures before it explains" is the question whether a tool call in turn 2
  touched the reported artifact at all.
- "It does not name the broken piece again" is a text check.
- "It does not geocode again" is a tool count.
- "The new result is not empty" is `raster_degenerate` from `geomeasure`.

What remains after that — "does it name the cause concretely" — is real
interpretation and stays with a human or a judge; the runner writes it into the log
unrated instead of inventing a verdict.

Separate from the runner so the checks are testable without a model (as `probes.py`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

#: "Regensburger Straße", "Regensburger Str.", "Regensburgerstraße" — a German street
#: name has several equally correct spellings, and which one arrives is decided by
#: OSM and the model, not by the test case. A check for the exact string therefore
#: measures the spelling instead of the behaviour: it fails although the agent fetched
#: exactly the right street. The same kind of error as a criterion left standing
#: after a change of place — a guaranteed false verdict that says nothing about the
#: agent.
_STREET_SUFFIX = re.compile(r"str(asse)?\.?")
_NOISE = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    """Reduce to the form in which two spellings of the same name are equal.

    Lower case, ß→ss, every street suffix to ``str``, then everything but letters and
    digits removed. That collapses case, the abbreviation dot, split and joined
    spelling and the hyphen. Deliberately coarse: the check is meant to establish
    *whether* the street was touched, not how it was spelled.
    """
    lowered = text.casefold().replace("ß", "ss")
    return _NOISE.sub("", _STREET_SUFFIX.sub("str", lowered))

#: All supported check kinds. Keep it small: whatever needs interpretation belongs in
#: the prose criteria, not here.
KINDS = (
    "tool_called",        # this tool ran in this turn
    "tool_not_called",    # this tool did NOT run in this turn
    # some call of the turn names this string in its arguments
    "tool_touched",
    "answer_omits",       # the turn's answer does NOT name this string
    "no_dead_path",       # every file path in the answer exists
    "no_flat_raster",     # no raster produced in this turn is empty/flat 0
    "fewer_calls_than",   # this turn needed fewer calls than that one
    # the most recently drawn map carries this geometry family (point/line/polygon)
    "map_shows_family",
)


class Turn:
    """What a turn left behind — the input of every check.

    ``tool_calls`` is the list ``(name, args)`` in order, ``tool_results`` the return
    values, ``answer`` the **validated** final answer (with gate note), ``written``
    the paths this turn produced.
    """

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt
        #: The turn hit the time cap — it left **no** session behind.
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
        """Whether **any** call of this turn names the term in its arguments.

        Compared normalised (:func:`_normalize`), not literally — otherwise the check
        measures the spelling instead of the behaviour.
        """
        want = _normalize(needle)
        return any(want in _normalize(json.dumps(args, ensure_ascii=False, default=str))
                   for _name, args in self.tool_calls)


def aborted_after(turns: list[Turn]) -> int | None:
    """After which turn the dialogue broke off — ``None`` if it ran through.

    A turn that hits the time cap is aborted; SelmaKit writes the session only for a
    complete run, so **it leaves nothing behind**. The next turn starts from zero —
    observed on 2026-09-01, when the agent answered "Gib die Karte als GeoTiff aus"
    with *"Da dies unser erster Austausch ist …"* ("since this is our first exchange").

    From that point no turn is meaningful any more: it is talking to a stranger. So
    the runner stops instead of producing numbers that measure something other than
    they claim (four of seven checks passed back then because the second turn did
    nothing — `fewer_calls_than: 0 gegen 28` was the clearest).
    """
    for i, t in enumerate(turns, 1):
        if t.timed_out:
            return i
    return None


def _turn(turns: list[Turn], index: Any) -> Turn | None:
    """Turn 1 is ``1``, not ``0`` — the criteria speak of turns, not indices."""
    try:
        i = int(index) - 1
    except (TypeError, ValueError):
        return None
    return turns[i] if 0 <= i < len(turns) else None


def check(assertion: dict, turns: list[Turn], *, workspace: Path) -> tuple[bool, str]:
    """Evaluate one check → ``(passed, reason)``."""
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
    """The check kinds that look beyond the turn, at files or other turns."""
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
        from chester.geofacts import is_raster
        from chester.geomeasure import raster_degenerate

        flat = [(Path(p).name, why) for p in turn.written
                if is_raster(p) and (why := raster_degenerate(p))]
        if not flat:
            return True, f"{len(turn.written)} Datei(en) erzeugt, kein leeres Raster"
        return False, "; ".join(f"{n}: {w}" for n, w in flat[:2])
    return False, "nicht ausgewertet"


def _map_family(family: str, workspace: Path) -> tuple[bool, str]:
    """Does the most recently drawn map really carry this geometry family?

    The check that was missing on 2026-09-01. Asked for were the **footprints** of
    four addresses; drawn were four points, because `native:intersection` had
    intersected the buildings with the geocoded points (polygon ∩ point = point). The
    map was made, the file existed, four features were in it, the columns carried
    `building=yes` — seven of seven checks green, and the picture showed four circles.

    What is asked is the **drawn** layer (`last_map.json`), not any file of the turn:
    the run had the right polygons on disk the whole time — just not on the map.
    """
    from chester.geofacts import geometry_families

    # Accept both forms: `probe.workspace()` — what `dialog.py` and the Test App pass
    # through — already returns the **geocache** directory, not the workspace root.
    # This function appended `geocache` a second time and searched in
    # `…/geocache/geocache/`. It only surfaced on 2026-09-05, in the **first** run in
    # which this check kind occurred at all: `render_map` had reported `ok: true`
    # twice, the file was there, and the check said "no map drawn". A check that has
    # never run is a claim.
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
    """The mechanical checks of a dialogue → ``(passed, log lines)``."""
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


#: Where the dialogue runs are kept — one line per dialogue and run.
HISTORY_PATH = Path(".chester") / "dialogs" / "history.jsonl"


def append_history(entry: dict, path: Path | None = None) -> None:
    """Append a result. Best effort — a write error never costs a run."""
    target = path or HISTORY_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_history(path: Path | None = None) -> list[dict]:
    """The archived dialogue runs, newest last."""
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


#: Which field a check kind needs besides ``kind`` and ``turn``. Derived from
#: `check()`, not maintained beside it: whoever reads a field there adds it here.
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
    """What a single assertion lacks — the legwork of `validate`."""
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
    """Everything broken in a dialogue case — an empty list means fine.

    Meant for the editor: a case with an unknown check kind or a missing field would
    otherwise fail only in the run, after twenty minutes of agent time, with a
    message about a line nobody believes they wrote. Here the same error costs one
    red line in the form.
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
