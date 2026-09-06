"""Die Gegenprobe: derselbe Benchmark-Fall, einmal mit Chester, einmal mit einem
nackten Frontier-Modell.

Die Kompensationsfrage (`doc/tool-compensation.md`) in ihrer kleinsten prüfbaren
Form. Ein **Test-Level-3**-Fall aus `agent-test-prompts.jsonl` läuft zweimal:

* **Chester** — volles Modell *plus* Werkzeugkasten, benotet wie immer vom Judge
  gegen die `success_criteria` des Falls. Diese Datei ändert daran nichts; sie nimmt
  das Urteil, das der Lauf ohnehin erzeugt.
* **Frontier nackt** — ein gehostetes Modell, **kein** Werkzeug, **keine**
  Chester-Instruktion, keine Sitzung. Es bekommt den Prompt des Falls und sonst
  nichts, und wird mit **derselben Rubrik** vom **selben** Judge benotet.

Damit ist der Maßstab über beide Zellen gleich — das ist der ganze Zweck, hier auf
Level 3 zu sitzen: Die Bank bringt Rubrik und Judge schon mit, Level 2 urteilt
absichtlich deterministisch am Artefakt und hätte für die nackte Zelle gar kein Maß.

**Was der Vergleich nicht kann.** Die Bank läuft `live`; ohne Werkzeuge kommt das
Frontier-Modell an keine Daten. Es beantwortet also „weiß es, was zu tun wäre?", nicht
„kann es es tun" — ein Rückstand misst zuerst die fehlenden Werkzeuge. Genau deshalb
sieht `doc/tool-compensation.md` §2 für die Zelle F− eigentlich *rohe Beschaffung
plus QGIS* vor. Der nackte Zuschnitt hier ist die schärfere, engere Frage; wer die
Zahlen liest, muss den Unterschied kennen.

**Warum die Claude API direkt und nicht pydantic-ai** (entschieden 2026-09-02).
Chesters Regel ist „the LLM layer is config-only" — die Gegenzelle bricht sie
bewusst. Der Gewinn: Die nackte Zelle ist wirklich nackt, ohne Framework, das
Nachrichten umformt oder Parameter setzt, und anbieterspezifische Fähigkeiten
(adaptives Denken, `effort`, Tokenabrechnung je Lauf) stehen unverstellt zur
Verfügung. Der Preis, und er gehört beim Lesen der Zahlen dazu: Die zwei Zellen
unterscheiden sich jetzt nicht nur im Werkzeugkasten, sondern auch im Client. Die
Zelle bleibt deshalb so schmucklos wie möglich — ein Aufruf, kein Systemprompt,
keine Werkzeuge, keine Sampling-Parameter.

Das letzte Wort hat der Mensch: `record_comparison` schreibt das eigene Urteil mit,
getrennt vom Judge, damit sich beide hinterher gegeneinander lesen lassen.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agent_build import CONFIG_NAME, STATE_DIR

#: Wo die Vergleiche liegen — eine Zeile je Benchmark-Fall und Gegenprobe. Neben
#: `evals/history.jsonl`, nicht darin: dort steht je Zeile *ein* benoteter Lauf, hier
#: ein Paar samt menschlichem Urteil. Zwei Formen in einer Datei hätten jede
#: Auswertung zu einer Fallunterscheidung gemacht.
COMPARISON_PATH = Path(STATE_DIR) / "evals" / "frontier.jsonl"

#: Ausgabedeckel der nackten Zelle. Grosszuegig, weil adaptives Denken mit
#: hineinzaehlt: `max_tokens` begrenzt Denken **und** Antwort zusammen, ein knapper
#: Wert liefert also eine abgeschnittene Antwort mit `stop_reason: max_tokens`.
_BARE_MAX_TOKENS = 32000


def frontier_model_name() -> str:
    """Der ``evals.frontier_model``-String aus der Config (eigener Block, best effort)."""
    try:
        cfg = json.loads((Path(STATE_DIR) / CONFIG_NAME).read_text(encoding="utf-8"))
        return ((cfg.get("evals") or {}).get("frontier_model") or "").strip()
    except (OSError, ValueError):
        return ""


def bare_client(model_name: str, timeout_s: float):
    """Ein Anthropic-Client, sonst nichts.

    **Direkt gegen die Claude API**, nicht über pydantic-ai wie der Rest von Chester
    (Entscheidung 2026-09-02). Der Preis dieser Wahl steht im Modul-Docstring; der
    Gewinn ist, dass die nackte Zelle wirklich nackt ist — kein Framework, das
    Nachrichten umschreibt, Werkzeuge einhängt oder Parameter setzt.

    Schlüsselauflösung übernimmt das SDK: ``ANTHROPIC_API_KEY``, sonst
    ``ANTHROPIC_AUTH_TOKEN``, sonst ein Profil aus ``ant auth login``. ``load_dotenv``
    holt vorher die ``.env`` dazu — `test_app.py` rief es als einziger Runner nicht,
    hätte den Schlüssel also nie gesehen.
    """
    from anthropic import AsyncAnthropic
    from dotenv import load_dotenv

    if not model_name_is_set(model_name):
        raise ValueError(
            f"kein Frontier-Modell gesetzt — `evals.frontier_model` in "
            f"{STATE_DIR}/{CONFIG_NAME} eintragen (z. B. \"claude-opus-4-8\") "
            f"und ANTHROPIC_API_KEY in .env hinterlegen."
        )
    load_dotenv()
    return AsyncAnthropic(timeout=timeout_s, max_retries=2)


def model_name_is_set(model_name: str) -> bool:
    """Ist ein Modellname gesetzt? (eigene Funktion, damit die UI dasselbe fragt)"""
    return bool((model_name or "").strip())


def bare_model_id(model_name: str) -> str:
    """Der Modell-String für die Claude API — ohne Anbieter-Präfix.

    In der Config darf ``anthropic/claude-opus-4-8`` stehen (die Schreibweise, die
    SelmaKits ``build_model`` erwartet und die auch der Judge benutzt). Die Claude API
    will den nackten Namen. Beides zuzulassen erspart die Fehlerquelle, dass ein
    Config-Eintrag je nach Verbraucher anders aussehen muss.
    """
    name = (model_name or "").strip()
    return name.split("/", 1)[1] if name.startswith("anthropic/") else name


async def run_bare(model_name: str, prompt: str, timeout_s: float) -> dict:
    """Den Prompt einmal stellen. Kein Werkzeug, kein Systemprompt, keine Sitzung.

    **Gestreamt**, wie es die Claude-API-Referenz für alles mit langer Ein- oder
    Ausgabe vorsieht: Ein nicht gestreamter Aufruf mit grossem ``max_tokens`` läuft
    in HTTP-Timeouts. ``get_final_message()`` gibt danach die vollständige Antwort.

    **Adaptives Denken ist an**, und das ist kein Zusatz, sondern Gleichstand:
    Chesters eigenes Modell läuft mit ``"thinking": "high"`` aus der Config. Eine
    Zelle ohne Denken gegen eine mit zu stellen, würde eine zweite Variable
    einführen. ``temperature`` und Geschwister werden **nicht** gesetzt — auf
    Opus 4.8 sind sie entfernt und quittieren mit 400.

    Gibt Antworttext, Dauer, Abbruchgrund und Tokenverbrauch zurück; der Verbrauch
    ist die Grundlage für die Kostenschätzung, die Phase KO vor den Messläufen
    verlangt.
    """
    import anthropic

    client = bare_client(model_name, timeout_s)
    started = time.monotonic()
    try:
        async with client.messages.stream(
            model=bare_model_id(model_name),
            max_tokens=_BARE_MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = await stream.get_final_message()
    except anthropic.APITimeoutError:
        return {"answer": "", "duration_s": time.monotonic() - started,
                "stop_reason": "timeout", "error": f"Zeitdeckel {timeout_s:.0f}s", "usage": {}}
    except anthropic.APIStatusError as exc:
        return {"answer": "", "duration_s": time.monotonic() - started,
                "stop_reason": "error", "error": f"{type(exc).__name__}: {exc}", "usage": {}}
    except anthropic.APIConnectionError as exc:
        return {"answer": "", "duration_s": time.monotonic() - started,
                "stop_reason": "error", "error": f"Netzfehler: {exc}", "usage": {}}

    # stop_reason **vor** content lesen: Bei einer Absage ist content leer oder
    # abgeschnitten, und ein blindes content[0] würde hier abstürzen.
    text = "".join(b.text for b in message.content if b.type == "text")
    usage = message.usage
    return {
        "answer": text,
        "duration_s": time.monotonic() - started,
        "stop_reason": message.stop_reason,
        "error": "" if message.stop_reason in ("end_turn", "max_tokens") else str(
            getattr(message, "stop_details", "") or message.stop_reason),
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        },
    }


async def judge_bare_run(judge_members, test: dict, prompt: str, model_name: str,
                         timeout_s: float) -> dict:
    """Die nackte Zelle: Prompt stellen, Antwort mit der Rubrik des Falls benoten.

    ``judge_members`` und ``test`` sind dieselben, mit denen der Chester-Lauf gerade
    benotet wurde — das ist die Bedingung dafür, dass die zwei Zahlen überhaupt
    nebeneinander stehen dürfen. ``tools`` ist leer und bleibt es: Die Zelle hat keine,
    also ist auch die Coverage über beide Zellen keine gemeinsame Kennzahl und wird
    hier nicht geführt.
    """
    from testprompt import judge_panel_run

    run = await run_bare(model_name, prompt, timeout_s)
    cell = {
        "model": model_name,
        "duration_s": round(run["duration_s"], 1),
        "answer": run["answer"],
        "stop_reason": run["stop_reason"],
        "usage": run["usage"],
    }
    if not run["answer"].strip():
        # Kein Urteil ohne Antwort: `passed: None` heisst **unbenotet**, nicht
        # durchgefallen. Ein Netzfehler als FAIL zu zaehlen faelschte den Vergleich
        # zugunsten der Zelle, die lief.
        return {**cell, "passed": None,
                "reason": run["error"] or "keine Antwort", "criteria": []}
    verdict, _cov, _missing, _effort, agreement = await judge_panel_run(
        judge_members, test, prompt, [], run["answer"])
    return {**cell,
            "passed": bool(verdict.passed),
            "reason": verdict.reason,
            "criteria": [{"text": c.text, "passed": bool(c.passed)}
                         for c in verdict.criteria],
            "panel": agreement}


def comparison_record(test: dict, prompt: str, judge_name: str,
                      chester: dict, frontier: dict) -> dict:
    """Beide Zellen zu einem archivierbaren Datensatz zusammenfassen.

    ``chester`` ist das Urteil, das der Lauf ohnehin erzeugt hat (dieselbe Form wie
    ``RunResult["verdict"]``), ``frontier`` das aus :func:`judge_bare_run`.
    """
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "test_id": test["id"],
        "category": test.get("category", ""),
        "prompt_de": prompt,
        "judge_model": judge_name,
        "chester": {
            "model": chester.get("model", ""),
            "passed": chester.get("passed"),
            "reason": chester.get("reason", ""),
            "coverage": chester.get("coverage"),
            "duration_s": chester.get("duration_s"),
            "answer": chester.get("answer", ""),
            "criteria": [{"text": txt, "passed": bool(ok)}
                         for txt, ok in (chester.get("criteria") or [])],
        },
        "frontier": frontier,
    }


def record_comparison(entry: dict, path: Path | None = None) -> None:
    """Einen Vergleich anhängen (Judge-Urteile **und** das Urteil des Menschen)."""
    target = path or COMPARISON_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_comparisons(path: Path | None = None) -> list[dict]:
    """Alle Vergleiche, älteste zuerst. Fehlende Datei → leere Liste."""
    target = path or COMPARISON_PATH
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows
