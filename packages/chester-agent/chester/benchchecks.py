"""Mechanical checks for the benchmark bank — the verdict before the judge (Phase MB).

GISAgentBench (arXiv 2608.01645) and GeoNatureAgent (arXiv 2606.12821) score agents with
**no LLM judge**: tolerant comparisons against expected files and values, expected tool
calls, required and forbidden text. Chester's bank decided on prose criteria through a
judge — which erred (CC.2: an `place=` the judge did not see), judged a Qwen model with
a Qwen model, and cost a model switch per verdict. Meanwhile every result of the last
weeks was checked by hand: files opened, means recomputed, boundaries intersected.
This module does that by rule.

A bank record gains a ``checks`` list. A check is either one of the dialogue kinds
(`chester.dialogs` — ``tool_called``, ``tool_not_called``, ``answer_omits``,
``no_dead_path``, …, always on the run's single turn) or one of the artifact kinds here,
which open what the run produced:

* ``artifact_crs`` — the last output of ``tool`` has CRS ``crs``;
* ``artifact_count_in_range`` — it holds ``min``..``max`` features;
* ``artifact_inside`` — it lies inside the last output of ``boundary_tool``, at most
  ``max_km2`` outside (a buffer reaching past the city limit);
* ``answer_number_in_range`` — the answer names, after the label ``near``, a number in
  ``min``..``max``; with ``unit`` only a number carrying that unit counts — in a table row
  "| Großer Arbersee | 49.0985° N | … | 934,9 m |" the first number is the latitude. The
  label matches as a whole word: "Großer Arber" does not match "Großer Arbersee". ``unit``
  takes alternatives ("m|Meter") — an answer said "105 **Meter**".

A ``tool`` may name alternatives as the bank's ``tools_expected`` does (``a|b``): the
rubric asks for a buffer, not for one buffer tool (`qgis_buffer` passed on 2026-08-24).
chester-team runs count the tools inside their ressorts, as `testprompt.py` does.

Windows, not exact values: live data drifts, and a check that fails on a re-mapped
school measures OSM, not the agent.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from chester import dialogs

ARTIFACT_KINDS = ("artifact_crs", "artifact_count_in_range", "artifact_inside",
                  "answer_number_in_range")

_NUMBER = re.compile(r"-?\d{1,3}(?:[.\u202f ]\d{3})+(?!\d)(?:,\d+)?|-?\d+(?:[.,]\d+)?")


def turn_from_session(session_key: str, sessions_dir: str | Path) -> dialogs.Turn:
    """The run of ``session_key`` as one dialogue turn: calls, results, outputs, answer.

    Calls inside a chester-code `run_code` program count as calls (the session is read
    flattened), so `tool_called: fetch_dgm1` means the same for agent and coder.
    """
    from chester.runtime.codemode import load_flat_session

    messages = load_flat_session(sessions_dir, session_key)
    if not messages:  # no run is not a failed run — say so instead of 0/N
        raise ValueError(f"keine Sitzung {session_key!r} in {sessions_dir}")
    prompt = next((p.get("content", "") for m in messages for p in m.get("parts", [])
                   if p.get("part_kind") == "user-prompt"), "")
    turn = dialogs.Turn(prompt if isinstance(prompt, str) else str(prompt))
    answer: list[str] = []
    for msg in messages:
        texts = []
        for part in msg.get("parts", []):
            kind = part.get("part_kind")
            if kind == "tool-call":
                turn.tool_calls.append((part.get("tool_name", "?"), _args(part.get("args"))))
            elif kind == "tool-return":
                content = part.get("content")
                turn.tool_results.append((part.get("tool_name", "?"), content))
                if isinstance(content, dict) and isinstance(content.get("output"), str):
                    turn.written.append(content["output"])
            elif kind == "text" and part.get("content"):
                texts.append(part["content"])
        if texts:
            answer = texts  # the final answer is the last response that carried text
    turn.answer = "\n".join(answer).strip()
    # chester-team: the tools inside a ressort count as this run's calls (as in testprompt)
    from chester.toolchoice import inner_tools

    turn.tool_calls.extend((name, None) for name in inner_tools([c for _n, c in turn.tool_results]))
    return turn


def _args(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def last_output(turn: dialogs.Turn, tool: str) -> str | None:
    """The written path of the last successful call of ``tool`` (``a|b``: of either).

    ``output`` for the wrapper tools, ``results.OUTPUT`` for the QGIS ones (`qgis_buffer`).
    """
    names = set(tool.split("|"))
    for name, content in reversed(turn.tool_results):
        if name not in names or not isinstance(content, dict) or not content.get("ok"):
            continue
        out = content.get("output") or (content.get("results") or {}).get("OUTPUT")
        if isinstance(out, str):
            return out
    return None


def _open(path: str, workspace: Path) -> Path | None:
    """``path`` as written, or — for a copied workspace — the same file under ``workspace``."""
    p = Path(path)
    if p.exists():
        return p
    tail = path.split("geocache/", 1)[-1]
    for candidate in (workspace / tail, workspace / Path(path).name):
        if candidate.exists():
            return candidate
    return None


def _layer(turn: dialogs.Turn, tool: str, workspace: Path):
    """The last output of ``tool``: a GeoDataFrame, or for a raster its rasterio profile."""
    import geopandas as gpd

    out = last_output(turn, tool)
    if out is None:
        return None, f"kein erfolgreicher Aufruf von {tool}"
    path = _open(out, workspace)
    if path is None:
        return None, f"{tool} schrieb {out}, die Datei fehlt"
    if path.suffix.lower() in (".tif", ".tiff"):
        import rasterio

        with rasterio.open(path) as src:
            return dict(src.profile), None
    return gpd.read_file(path), None


def answer_number(answer: str, near: str, unit: str = "", window: int = 160) -> float | None:
    """The first number after ``near`` in ``answer`` — with ``unit``, the first carrying it."""
    label = re.search(re.escape(near) + r"(?![a-zäöüß])", answer, re.IGNORECASE)
    if label is None:
        return None
    end = min(len(answer), label.end() + window)
    if unit:
        units = "|".join(re.escape(u) for u in unit.split("|"))
        pattern = re.compile(rf"({_NUMBER.pattern})\s*(?:{units})(?![a-zäöüß])",
                             re.IGNORECASE)
    else:
        pattern = re.compile(f"({_NUMBER.pattern})")
    match = pattern.search(answer, label.end(), end)
    if not match:
        return None
    text = match.group(1).replace("\u202f", "").replace(" ", "")
    if "," in text:  # German: 1.452,7
        text = text.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", text):  # German thousands: 1.452
        text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


def check_artifact(a: dict, turn: dialogs.Turn, workspace: Path) -> tuple[bool, str]:
    """One artifact check → ``(passed, reason)``."""
    kind = a["kind"]
    if kind == "answer_number_in_range":
        value = answer_number(turn.answer, a["near"], a.get("unit", ""))
        if value is None:
            return False, f"nach „{a['near']}“ steht in der Antwort keine Zahl"
        ok = a["min"] <= value <= a["max"]
        return ok, f"„{a['near']}“: {value:g} (Soll {a['min']}–{a['max']})"
    gdf, why = _layer(turn, a["tool"], workspace)
    if gdf is None:
        return False, why or ""
    if isinstance(gdf, dict) and kind != "artifact_crs":  # a raster profile
        return False, f"{a['tool']} schrieb ein Raster — {kind} prüft Vektorebenen"
    if kind == "artifact_crs":
        crs = gdf.get("crs") if isinstance(gdf, dict) else gdf.crs
        epsg = crs.to_epsg() if crs else None
        return f"EPSG:{epsg}" == a["crs"], f"{a['tool']}: EPSG:{epsg} (Soll {a['crs']})"
    if kind == "artifact_count_in_range":
        n = len(gdf)
        return a["min"] <= n <= a["max"], f"{a['tool']}: {n} Objekte (Soll {a['min']}–{a['max']})"
    if kind == "artifact_inside":
        area, why = _layer(turn, a["boundary_tool"], workspace)
        if area is None:
            return False, why or ""
        metric = gdf.to_crs(25832).union_all()
        limit = area.to_crs(25832).union_all().buffer(1)
        outside = metric.difference(limit).area / 1e6
        why = f"{a['tool']}: {outside:.2f} km² außerhalb (Soll ≤ {a['max_km2']})"
        return outside <= a["max_km2"], why
    return False, f"unbekannte Prüfart {kind!r}"


def evaluate(test: dict, turn: dialogs.Turn, workspace: Path) -> tuple[bool, list[str]]:
    """All ``checks`` of a bank record on its run → ``(passed, log lines)``."""
    lines, passed = [], True
    for a in test.get("checks", []):
        if a.get("kind") in ARTIFACT_KINDS:
            ok, why = check_artifact(a, turn, workspace)
        elif a.get("kind") == "tool_called" and "|" in a.get("tool", ""):
            hits = [t for t in a["tool"].split("|") if t in turn.tools]
            ok, why = bool(hits), (", ".join(f"{turn.tools.count(t)}× {t}" for t in hits)
                                   or f"keins von {a['tool']}")
        else:
            ok, why = dialogs.check({**a, "turn": 1}, [turn], workspace=workspace)
        passed &= ok
        lines.append(f"  {'✓' if ok else '✗'} {a['kind']}: {why}")
    return passed, lines


_REQUIRED = {"artifact_crs": ("tool", "crs"), "artifact_count_in_range": ("tool", "min", "max"),
             "artifact_inside": ("tool", "boundary_tool", "max_km2"),
             "answer_number_in_range": ("near", "min", "max")}


def validate(test: dict) -> list[str]:
    """What is broken in a record's ``checks`` — empty means fine."""
    out = []
    for j, a in enumerate(test.get("checks", []), 1):
        kind = a.get("kind")
        if kind in ARTIFACT_KINDS:
            out += [f"Prüfung {j} ({kind}): Feld {f!r} fehlt"
                    for f in _REQUIRED[kind] if f not in a]
        else:
            out += dialogs._assertion_problems({**a, "turn": 1}, j, 1)
    return out


#: One line per mechanically checked run — beside the judge's `history.jsonl` (MB.3).
CHECKS_HISTORY = Path(".chester") / "evals" / "checks.jsonl"

#: Chester's own run log (`RunLogCapability`): written live, so it survives a run that
#: never finished — but appended to across runs, and long fields are cut at 1 200 chars.
RUN_LOG_DIR = Path(".chester") / "logs" / "runs"


def turn_from_runlog(session_key: str, since: datetime, log_dir: str | Path = RUN_LOG_DIR,
                     ) -> dialogs.Turn:
    """The run of ``session_key`` that started at ``since``, read from the run log.

    The fallback for a run that left no session: SelmaKit persists one only for a run
    that completed — the `inspect_map` crash of 2026-11-07 left nothing to check. Only
    records from ``since`` on count (the log holds every earlier run of the key too).
    A result cut at 1 200 chars no longer parses and yields no output path.
    """
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_key)[:80]
    path = Path(log_dir) / f"{safe}.jsonl"
    if not path.exists():
        raise ValueError(f"weder Sitzung noch Laufprotokoll für {session_key!r}")
    turn = dialogs.Turn("")
    texts: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
            when = datetime.fromisoformat(r["t"])
        except (ValueError, KeyError):
            continue
        if when < since:
            continue
        if r.get("kind") == "call":
            turn.tool_calls.append((r.get("tool", "?"), _args(r.get("args"))))
            texts = []  # the answer is the text after the last call
        elif r.get("kind") == "result":
            content = _args(r.get("result"))
            turn.tool_results.append((r.get("tool", "?"), content))
            if isinstance(content, dict) and isinstance(content.get("output"), str):
                turn.written.append(content["output"])
        elif r.get("kind") == "text" and r.get("text"):
            texts.append(r["text"])
    if not turn.tool_calls and not texts:
        raise ValueError(f"keine Einträge seit {since:%H:%M:%S} im Laufprotokoll {path}")
    turn.answer = "\n".join(texts).strip()
    return turn


def run_report(test: dict, session_key: str, sessions_dir: str | Path,
               workspace: str | Path | None = None, *, duration_s: float | None = None,
               model: str = "") -> str:
    """The checks of ``test`` on the run just finished, as text — never raises.

    Called by `testprompt.py` right after a run, before any judge: the files are still
    in the GeoCache then. Without a session (a run that never finished) and with
    ``duration_s``, the run log stands in. Every evaluated run is appended to
    :data:`CHECKS_HISTORY`. A failure to check is reported, not raised — the run itself
    already happened and must not be lost to a broken check.
    """
    from chester.workspace import DEFAULT_WORKSPACE, resolve_path

    ws = Path(workspace) if workspace else Path(resolve_path("x.gpkg", DEFAULT_WORKSPACE)).parent
    source = "Sitzung"
    try:
        try:
            turn = turn_from_session(session_key, sessions_dir)
        except ValueError:
            if duration_s is None:
                raise
            since = datetime.now() - timedelta(seconds=duration_s + 60)
            turn, source = turn_from_runlog(session_key, since), "Laufprotokoll (gekürzt)"
        passed, lines = evaluate(test, turn, ws)
    except Exception as exc:  # noqa: BLE001 — a broken check must not cost the run
        return f"\n[checks] nicht ausgewertet: {type(exc).__name__}: {exc}"
    ok = sum(line.lstrip().startswith("✓") for line in lines)
    _append({"t": datetime.now().isoformat(timespec="seconds"), "test": test.get("id"),
             "model": model, "passed": passed, "ok": ok, "n": len(lines), "source": source,
             "lines": [line.strip() for line in lines]})
    head = f"\n[checks] {'PASS' if passed else 'FAIL'} ({ok}/{len(lines)})"
    return head + ("" if source == "Sitzung" else f" — aus dem {source}") + "\n" + "\n".join(lines)


def _append(entry: dict, path: Path = CHECKS_HISTORY) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # a log that cannot be written must not cost the report


_PROTO_CALL = re.compile(r"│ → ([a-z_0-9]+)\((.*)\)\s*$")
_PROTO_RESULT = re.compile(r"│ ← ([a-z_0-9]+): (.*)$")
_PROTO_TEXT = re.compile(r"│ (?![→←])(.*)$")


def turn_from_protocol(path: str | Path) -> dialogs.Turn:
    """A judged run, rebuilt from its kept protocol (`.chester/evals/runs/*.log`) — MB.3.

    Sessions of old runs are gone; the protocol of every judged run is not. Its lines
    are cut at display width, so a long result may not parse and yields no output path.
    """
    turn = dialogs.Turn("")
    texts: list[str] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("[run]"):
            break
        if match := _PROTO_CALL.search(line):
            turn.tool_calls.append((match.group(1), _args(match.group(2))))
            texts = []
        elif match := _PROTO_RESULT.search(line):
            content = _args(match.group(2))
            turn.tool_results.append((match.group(1), content))
            texts = []
        elif (match := _PROTO_TEXT.search(line)) and match.group(1).strip():
            texts.append(match.group(1))
    turn.answer = "\n".join(texts).strip()
    return turn


def compare_with_judge(records: list[dict], bank: dict[str, dict]) -> list[dict]:
    """Each judged run of a prompt with checks: the mechanical verdict beside the judge's.

    Artifact checks whose file is gone count as *not checkable* and stay out of the
    verdict — a deleted GeoCache is no failure of the run. Runs with QGIS on are left out:
    their buffers and isochrones ran through `qgis_run`, some checked tools did not exist
    yet. The history's ``tools_called`` adds what the protocol does not show — the tools
    inside a chester-code program, the ressorts' tools.
    """
    from chester.failclass import _list

    rows = []
    for rec in records:
        test = bank.get(str(rec.get("test_id")))
        log = rec.get("log")
        called = _list(rec.get("tools_called"))
        if (not test or not test.get("checks") or not log or not Path(log).exists()
                or str(rec.get("use_qgis")).lower() == "true"
                or any(n.startswith("qgis_") for n in called)):
            continue
        turn = turn_from_protocol(log)
        hidden = {n for n in called if n not in set(turn.tools)}  # ran inside a program
        turn.tool_calls += [(n, None) for n in hidden]
        verdicts = []
        for a in test["checks"]:
            ok, why = (check_artifact(a, turn, Path("."))
                       if a.get("kind") in ARTIFACT_KINDS else
                       dialogs.check({**a, "turn": 1}, [turn], workspace=Path(".")))
            if a.get("kind") in ARTIFACT_KINDS and ("fehlt" in why or (
                    "kein erfolgreicher" in why and set(a["tool"].split("|")) & hidden)):
                continue  # file gone, or the tool ran where the protocol cannot see
            if a.get("kind") == "tool_touched" and hidden and not ok:
                continue  # its arguments were inside a program
            verdicts.append((a["kind"], ok, why))
        if not verdicts:
            continue
        rows.append({"test": test["id"], "ts": str(rec.get("ts", ""))[:16],
                     "model": rec.get("model"), "judge": str(rec.get("passed")).lower() == "true",
                     "mechanical": all(ok for _k, ok, _w in verdicts),
                     "failed": [f"{k}: {w}" for k, ok, w in verdicts if not ok]})
    return rows
