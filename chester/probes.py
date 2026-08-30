"""Die Prüfungen der Test-Level-2-Proben — rein, ohne Modell, ohne Netz.

Test-Level 2 misst am **erzeugten Artefakt** und an den **Rückgabewerten der
Werkzeuge**, nie am Antworttext (`doc/test-levels.md`). Genau diese Auswertung steht
hier: eine Handvoll Prüfarten, jede ein `assert` auf eine Datei oder auf eine Zahl,
die ein Werkzeug zurückgemeldet hat. Kein Judge, keine Heuristik, kein Urteil.

Getrennt vom Runner, weil eine Prüflogik, die man nur mit einem laufenden Modell
testen kann, selbst ungeprüft bleibt.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

#: Alle unterstützten Prüfarten — bewusst klein gehalten.
KINDS = (
    "output_exists",  # die Datei wurde geschrieben
    "no_output",      # es wurde KEINE passende Datei geschrieben (Absage-Fälle)
    "crs_metric",     # Ausgabe in einem projizierten CRS (nicht Grad)
    "crs_epsg",       # Ausgabe in genau diesem EPSG-Code
    "features",       # Objektzahl
    "area_m2",        # Gesamtfläche in Quadratmetern
    "no_nulls",       # eine Spalte ohne fehlende Werte
    "value_seen",     # irgendein Werkzeug hat diese Zahl zurückgegeben
)


def _rel(expect: float, got: float) -> float:
    return abs(got - expect) / abs(expect) if expect else abs(got)


def _numbers(obj: Any, _depth: int = 0) -> list[float]:
    """Jede Zahl aus einer Werkzeug-Rückgabe, beliebig tief verschachtelt."""
    if _depth > 6:
        return []
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in _numbers(v, _depth + 1)]
    if isinstance(obj, (list, tuple)):
        return [n for v in obj for n in _numbers(v, _depth + 1)]
    if isinstance(obj, str):
        # Zahlen in Fließtext bleiben außen vor: eine Prosa-Zahl ist kein Artefakt.
        return []
    return []


def _layer(path: Path):
    import geopandas as gpd

    return gpd.read_file(path)


def check(assertion: dict, *, workspace: Path, tool_results: list[Any]) -> tuple[bool, str]:
    """Eine Prüfung auswerten → ``(bestanden, Begründung)``.

    ``workspace`` ist das Verzeichnis, in dem die Ausgaben landen (der GeoCache);
    ``tool_results`` sind die Rückgabewerte aller Werkzeugaufrufe dieses Laufs.
    """
    kind = assertion.get("kind")
    if kind not in KINDS:
        return False, f"unbekannte Prüfart {kind!r}"

    if kind == "no_output":
        hits = sorted(p.name for p in workspace.glob(assertion["glob"]))
        return (not hits), ("keine Datei erzeugt" if not hits else f"erzeugt: {', '.join(hits)}")

    if kind == "value_seen":
        expect = float(assertion["expect"])
        tol_abs = assertion.get("tol_abs")
        seen = _numbers(tool_results)
        for got in seen:
            ok = abs(got - expect) <= tol_abs if tol_abs else _rel(expect, got) <= assertion["tol"]
            if ok and not math.isnan(got):
                return True, f"{got:,.4f} gefunden"
        near = min(seen, key=lambda g: abs(g - expect), default=None)
        return False, f"{expect:,.4f} in keiner Werkzeug-Rückgabe (nächster Wert: {near})"

    path = workspace / assertion["path"]
    if kind == "output_exists":
        return path.is_file(), ("vorhanden" if path.is_file() else f"fehlt: {assertion['path']}")
    if not path.is_file():
        return False, f"Datei fehlt: {assertion['path']}"

    try:
        gdf = _layer(path)
    except Exception as exc:  # noqa: BLE001 — eine unlesbare Ausgabe ist ein Fehlschlag
        return False, f"nicht lesbar: {type(exc).__name__}"
    return _check_layer(kind, assertion, gdf)


def _check_layer(kind: str, assertion: dict, gdf) -> tuple[bool, str]:
    """Die Prüfarten, die eine gelesene Ebene brauchen."""
    if kind == "crs_metric":
        if gdf.crs is None:
            return False, "kein CRS"
        return (not gdf.crs.is_geographic), f"CRS {gdf.crs.to_string()}"
    if kind == "crs_epsg":
        got = gdf.crs.to_epsg() if gdf.crs else None
        return got == assertion["expect"], f"EPSG:{got}"
    if kind == "features":
        return len(gdf) == assertion["expect"], f"{len(gdf)} Objekte"
    if kind == "area_m2":
        if gdf.crs is None or gdf.crs.is_geographic:
            return False, "Fläche in einem geographischen CRS ist keine Fläche"
        got = float(gdf.geometry.area.sum())
        rel = _rel(float(assertion["expect"]), got)
        return rel <= assertion["tol"], f"{got:,.1f} m² (Abweichung {rel:.1%})"
    if kind == "no_nulls":
        col = assertion["column"]
        if col not in gdf.columns:
            return False, f"Spalte {col!r} fehlt ({', '.join(map(str, gdf.columns[:8]))})"
        n = int(gdf[col].isna().sum())
        return n == 0, ("keine Nullwerte" if n == 0 else f"{n} Nullwerte in {col!r}")
    return False, "nicht ausgewertet"


def evaluate(task: dict, *, workspace: Path, tool_results: list[Any]) -> tuple[bool, list[str]]:
    """Alle Prüfungen einer Aufgabe → ``(bestanden, Zeilen für das Protokoll)``."""
    lines, passed = [], True
    for a in task.get("assertions", []):
        ok, why = check(a, workspace=workspace, tool_results=tool_results)
        passed &= ok
        lines.append(f"  {'✓' if ok else '✗'} {a['kind']}: {why}")
    return passed, lines


def effective_timeout(task: dict, default_s: float) -> float:
    """Der Zeitdeckel dieser Probe: ihr eigener, sonst der vorgegebene.

    Eine Absage-Probe braucht mehr Luft als eine Rechenaufgabe: `ndvi-without-nir`
    handelte am 2026-08-30 sachlich richtig (es entstand keine Datei), sagte es aber
    nicht in 180 s — durchgefallen war damit die Geduld des Prüfstands, nicht das
    Modell. Wer einen Fall länger laufen lassen will, schreibt das in die Aufgabe,
    wo es neben der Falle steht und begründet werden kann.
    """
    own = task.get("timeout_s")
    return float(own) if own else float(default_s)


#: Wo die Ergebnisse der Proben liegen — eine Zeile je Probe und Lauf.
HISTORY_PATH = Path(".chester") / "probes" / "history.jsonl"


def append_history(entry: dict, path: Path | None = None) -> None:
    """Ein Ergebnis anhängen. Best effort — ein Schreibfehler kostet keinen Lauf."""
    target = path or HISTORY_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_history(path: Path | None = None, limit: int | None = None) -> list[dict]:
    """Die archivierten Ergebnisse, neueste zuletzt."""
    target = path or HISTORY_PATH
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue  # eine kaputte Zeile darf den Bericht nicht kosten
    return rows[-limit:] if limit else rows


def latest_per_probe(rows: list[dict]) -> dict[str, dict]:
    """Je Probe der jüngste Eintrag — die Übersicht, die eine UI zeigen will."""
    out: dict[str, dict] = {}
    for row in rows:
        rid = row.get("id")
        if rid and (rid not in out or row.get("ts", "") >= out[rid].get("ts", "")):
            out[rid] = row
    return out
