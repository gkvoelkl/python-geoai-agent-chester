"""The checks of the Test-Level-2 probes — pure, no model, no network.

Test-Level 2 measures the **produced artifact** and the **tools' return values**, never
the answer text (`doc/test-levels.md`). This is that evaluation: a handful of check
kinds, each an `assert` on a file or on a number a tool reported. No judge, no
heuristic, no verdict.

Separate from the runner, because check logic that can only be tested with a running
model stays untested itself.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from chester.toolvalues import numbers, ressort_numbers

#: Alle unterstützten Prüfarten — bewusst klein gehalten.
KINDS = (
    "output_exists",  # the file was written
    "no_output",      # NO matching file was written (refusal cases)
    "crs_metric",     # output in a projected CRS (not degrees)
    "crs_epsg",       # output in exactly this EPSG code
    "features",       # Objektzahl
    "area_m2",        # Gesamtfläche in Quadratmetern
    "no_nulls",       # a column without missing values
    "value_seen",     # irgendein Werkzeug hat diese Zahl zurückgegeben
)


def _rel(expect: float, got: float) -> float:
    return abs(got - expect) / abs(expect) if expect else abs(got)


def _layer(path: Path):
    import geopandas as gpd

    return gpd.read_file(path)


def check(assertion: dict, *, workspace: Path, tool_results: list[Any]) -> tuple[bool, str]:
    """Evaluate one check → ``(passed, reason)``.

    ``workspace`` is the directory the outputs land in (the GeoCache); ``tool_results``
    are the return values of every tool call of this run.
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
        # For the team the number is computed inside a ressort; its log holds it.
        seen = numbers(tool_results) + ressort_numbers(tool_results)
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
    except Exception as exc:  # noqa: BLE001 — an unreadable output is a failure
        return False, f"nicht lesbar: {type(exc).__name__}"
    return _check_layer(kind, assertion, gdf)


def _check_layer(kind: str, assertion: dict, gdf) -> tuple[bool, str]:
    """The check kinds that need a layer read."""
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
    """All checks of one task → ``(passed, lines for the protocol)``."""
    lines, passed = [], True
    for a in task.get("assertions", []):
        ok, why = check(a, workspace=workspace, tool_results=tool_results)
        passed &= ok
        lines.append(f"  {'✓' if ok else '✗'} {a['kind']}: {why}")
    return passed, lines


def timeout_decides(task: dict) -> bool:
    """Does a broken time limit count as a failure although the checks pass?

    Only when the probe aims at **saying** something. For a refusal (`ndvi-without-nir`)
    that is the answer: whoever has not said after seven minutes that three bands make
    no NDVI has not refused — there `requires_finish: true` stands.

    For a computation the artifact is the answer. `union-not-sum` delivered exactly
    100,000 m² on 2026-08-31 (deviation 0.0 %) and was still graded FAIL, because the
    model was still writing when the limit fell. That measured the bench's patience,
    not the model's ability. Since then the limit bounds the **time**, not the verdict
    — and the overrun still stands in the protocol, so nobody misses it.
    """
    return bool(task.get("requires_finish"))


def effective_timeout(task: dict, default_s: float) -> float:
    """This probe's time limit: its own, otherwise the default.

    A refusal probe needs more room than a computation: `ndvi-without-nir` acted
    correctly on 2026-08-30 (no file was produced) but did not say so within 180 s —
    what failed was the bench's patience, not the model. Whoever wants a case to run
    longer writes it into the task, where it stands beside the trap and can be argued.

    **The own value applies unconditionally, even when it is smaller.** When the default
    rose to 480 s on 2026-09-01, the 420 s of `ndvi-without-nir` had silently become a
    shortening — the opposite of what was meant. The entry is therefore gone; no probe
    has its own limit today. Whoever sets one again checks it against
    `DEFAULT_TIMEOUT_S`.
    """
    own = task.get("timeout_s")
    return float(own) if own else float(default_s)


#: Where the probe results live — one line per probe and run.
HISTORY_PATH = Path(".chester") / "probes" / "history.jsonl"


def append_history(entry: dict, path: Path | None = None) -> None:
    """Append one result. Best effort — a write error never costs a run."""
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
            continue  # a broken line must not cost the report
    return rows[-limit:] if limit else rows


def latest_per_probe(rows: list[dict]) -> dict[str, dict]:
    """The latest entry per probe — the overview a UI wants to show."""
    out: dict[str, dict] = {}
    for row in rows:
        rid = row.get("id")
        if rid and (rid not in out or row.get("ts", "") >= out[rid].get("ts", "")):
            out[rid] = row
    return out
