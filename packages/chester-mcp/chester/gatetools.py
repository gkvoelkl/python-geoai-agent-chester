"""`validate_result` als **rahmenneutrale** Hülle — das Gate für fremde Clients.

Phase KM, Schritt 4. Chesters inhaltliche These ist nicht Funktionsbreite, sondern
Überprüfbarkeit: „Correctness is a loop phase", erzwungen von `chester/gate.py`. Über
MCP wird daraus „Correctness is a tool you may call" — der Server kann einen fremden
Client zu nichts zwingen.

**Dieser Wegfall ist der Messgegenstand der Zelle F+MCP, kein Umsetzungsmangel**
(`internal/chester-mcp.md` §5, Variante 3: Selbstauskunft je Schritt *plus* ein
ausdrückliches `validate_result`). Deshalb steht im Rückgabewert `enforced: false`,
unübersehbar, statt dass die Werkzeugbeschreibung Verbindlichkeit vortäuscht.

Eigenes Modul und nicht in `validationtools.py`: Dort stehen die *fachlichen* Prüfungen
(CRS, Topologie, Plausibilität, Querprüfung), die ein Agent mitten in der Arbeit ruft.
Hier steht die Schlussprüfung über das fertige Ergebnis — und sie hängt an `gate.py`,
dem einen Modul mit Sonderstatus.
"""

from __future__ import annotations

from collections.abc import Callable

from chester import gate


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """`validate_result`, an ``workspace`` gebunden."""
    ws = workspace

    def validate_result(paths: list[str], answer: str = "",
                        level: int = gate.DEFAULT_LEVEL) -> dict:
        """Check finished geo results before you report them, and get the findings.

        Call this **last**, after the files exist and you know what you are going to
        say — it checks both. ``paths`` are the files you produced, ``answer`` is the
        text you are about to send.

        Per file: empty layers, a missing CRS, geometry that does not match the
        declared type, index ranges, and at ``level`` 3 a stored area/length column
        against the real geometry plus redundancy. Over the answer: links that point
        nowhere, and claims about files that do not exist.

        Returns ``must_fix: true`` when something is wrong that would make the answer
        untrue, with one entry per finding (``path``, ``check``, ``severity``,
        ``problem``). ``enforced: false`` is part of the answer: nothing here stops
        you from reporting anyway — the decision is yours.

        ``checks_not_run`` names what this cannot see: the checks that need the whole
        run rather than its result.
        """
        try:
            return gate.inspect_result(paths, answer, workspace=ws, level=level)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return [validate_result]
