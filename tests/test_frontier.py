"""Tests für die Gegenprobe Chester ↔ nacktes Frontier-Modell.

Geprüft wird, was ohne Modell prüfbar ist: dass der Datensatz beide Zellen in
dieselbe Form bringt, das Archiv und die Absage ohne konfiguriertes Modell. Der
Vergleichslauf selbst braucht zwei Modelle und gehört damit nicht auf Test-Level 1.
"""

from __future__ import annotations

import json

import pytest

import frontier


def _bank_test():
    return {
        "id": "cycleway-length",
        "category": "4. Measuring",
        "prompt_de": "Wie lang sind die Radwege in Regensburg?",
        "success_criteria": ["Auf die Gemeindegrenze geclippt", "Metrisches CRS"],
        "tools_expected": ["geocode", "osm_features"],
    }


def test_the_record_puts_both_cells_on_the_same_rubric():
    """Ein Vergleich ist nur einer, wenn beide Zellen denselben Maßstab tragen."""
    chester = {"passed": True, "reason": "sauber geclippt", "coverage": 1.0,
               "duration_s": 620.0, "answer": "612 km", "model": "ollama/gemma4:26b-mlx",
               "criteria": [("Auf die Gemeindegrenze geclippt", True),
                            ("Metrisches CRS", True)]}
    frontier_cell = {"model": "anthropic/claude-opus-4-8", "duration_s": 12.0,
                     "answer": "Dafür bräuchte ich OSM-Daten.", "passed": False,
                     "reason": "kein Datenzugang",
                     "criteria": [{"text": "Auf die Gemeindegrenze geclippt",
                                   "passed": False}]}
    rec = frontier.comparison_record(_bank_test(), "Wie lang …?", "ollama/qwen3.8:27b-mlx",
                                     chester, frontier_cell)

    assert rec["test_id"] == "cycleway-length"
    assert rec["judge_model"] == "ollama/qwen3.8:27b-mlx"
    assert rec["chester"]["passed"] is True and rec["frontier"]["passed"] is False
    # Chesters Kriterien kommen als Tupel aus dem Lauf und müssen dieselbe Form
    # bekommen wie die der Gegenzelle — sonst liest keine Auswertung beide.
    assert rec["chester"]["criteria"][0] == {"text": "Auf die Gemeindegrenze geclippt",
                                             "passed": True}
    assert isinstance(rec["frontier"]["criteria"][0], dict)


def test_the_record_survives_a_run_without_criteria():
    rec = frontier.comparison_record(_bank_test(), "p", "j", {"passed": None}, {"passed": None})
    assert rec["chester"]["criteria"] == []


def test_comparisons_round_trip(tmp_path):
    p = tmp_path / "frontier.jsonl"
    frontier.record_comparison({"probe": "a", "human": {"verdict": "Chester"}}, p)
    frontier.record_comparison({"probe": "b", "human": {"verdict": "Frontier"}}, p)
    rows = frontier.read_comparisons(p)
    assert [r["probe"] for r in rows] == ["a", "b"]  # älteste zuerst
    assert rows[1]["human"]["verdict"] == "Frontier"


def test_reading_a_missing_archive_is_empty_not_an_error(tmp_path):
    assert frontier.read_comparisons(tmp_path / "nope.jsonl") == []


def test_a_broken_line_does_not_lose_the_rest(tmp_path):
    p = tmp_path / "frontier.jsonl"
    p.write_text('{"probe": "a"}\nkaputt\n{"probe": "c"}\n', encoding="utf-8")
    assert [r["probe"] for r in frontier.read_comparisons(p)] == ["a", "c"]


def test_no_configured_model_refuses_with_the_way_out():
    with pytest.raises(ValueError) as exc:
        frontier.bare_client("", 60.0)
    assert "evals.frontier_model" in str(exc.value)
    assert "ANTHROPIC_API_KEY" in str(exc.value)  # beide Schritte, nicht nur der erste


@pytest.mark.parametrize("configured, sent", [
    ("claude-opus-4-8", "claude-opus-4-8"),
    ("anthropic/claude-opus-4-8", "claude-opus-4-8"),   # SelmaKit-Schreibweise
    ("  claude-sonnet-5  ", "claude-sonnet-5"),
])
def test_the_provider_prefix_is_stripped_for_the_claude_api(configured, sent):
    """Die Config darf beide Schreibweisen tragen; die API kennt nur die nackte.

    Der Judge laeuft ueber SelmaKits `build_model` und braucht `anthropic/…`, die
    Gegenzelle spricht die Claude API direkt an und wuerde daran mit 404 scheitern.
    Ein Config-Eintrag, der je nach Verbraucher anders aussehen muss, waere eine
    Fehlerquelle ohne Gegenwert.
    """
    assert frontier.bare_model_id(configured) == sent


def test_frontier_model_name_survives_a_config_without_the_block(tmp_path, monkeypatch):
    monkeypatch.setattr(frontier, "STATE_DIR", str(tmp_path))
    (tmp_path / frontier.CONFIG_NAME).write_text(json.dumps({"model": {}}), encoding="utf-8")
    assert frontier.frontier_model_name() == ""


# ── Live-Log der nackten Zelle ───────────────────────────────────────────────
# Bis 2026-09-09 schrieb die Zelle gar nichts mit: die Antwort lebte allein in
# Streamlits `session_state`, ein geschlossener Tab warf einen bezahlten Aufruf
# samt Tokenzahlen weg. Geprüft wird hier, was ohne Modell prüfbar ist — dass
# geschrieben wird, *während* es läuft, und dass ein Fehlschlag eine Spur hat.


def test_the_log_is_readable_while_the_call_is_still_running(tmp_path):
    """Eine Zeile ist lesbar, sobald sie fertig ist — nicht erst am Ende."""
    path = tmp_path / "live.jsonl"
    log = frontier._LiveLog(path)
    log.write("start", model="claude-sonnet-5")
    log.text("text", "erste Zeile\nzweite ")

    kinds = [json.loads(line)["kind"] for line in path.read_text().splitlines()]
    assert kinds == ["start", "text"], (
        "die fertige Zeile steht noch nicht auf der Platte — das Log ist nicht live"
    )
    # Die angefangene zweite Zeile darf noch fehlen; erst close() gibt sie frei.
    log.close()
    texts = [json.loads(line).get("text") for line in path.read_text().splitlines()]
    assert texts[-1] == "zweite "


def test_a_very_long_line_does_not_stay_stuck_in_the_buffer(tmp_path):
    """Ohne Deckel bliebe ein Absatz ohne Zeilenumbruch bis zum Schluss unsichtbar."""
    path = tmp_path / "live.jsonl"
    log = frontier._LiveLog(path)
    log.text("text", "x" * (frontier._LOG_LINE_FLUSH + 1))
    assert path.exists() and path.read_text().strip(), (
        "eine lange Zeile ohne \\n wurde nicht ausgespült"
    )


def test_thinking_and_answer_stay_apart_in_the_log(tmp_path):
    """Denken ist nicht Antwort — der Judge sieht nur letztere."""
    path = tmp_path / "live.jsonl"
    log = frontier._LiveLog(path)
    log.text("thinking", "erst überlegen\n")
    log.text("text", "dann antworten\n")
    kinds = [json.loads(line)["kind"] for line in path.read_text().splitlines()]
    assert kinds == ["thinking", "text"]


def test_a_failed_call_still_leaves_a_trace(tmp_path):
    """Timeout und Netzfehler sind der Grund, warum das Log existiert."""
    path = tmp_path / "live.jsonl"
    log = frontier._LiveLog(path)
    log.text("text", "halbe Antwort")  # noch ungespült
    out = frontier._failed(log, 0.0, "timeout", "Zeitdeckel 300s")

    assert out["answer"] == "" and out["stop_reason"] == "timeout"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["text"] == "halbe Antwort", "abgeschnittener Text ging verloren"
    assert records[-1]["kind"] == "failed" and records[-1]["error"] == "Zeitdeckel 300s"


def test_a_broken_log_directory_does_not_kill_the_run(tmp_path):
    """Ein Beobachter, der den Lauf scheitern lässt, ist schlimmer als keiner."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("ich bin eine Datei")
    log = frontier._LiveLog(blocker / "sub" / "live.jsonl")
    log.write("start", model="x")
    log.text("text", "a\n")
    log.close()  # kein Fehler, keine Ausnahme


def test_the_log_lands_beside_the_chester_protocol():
    """Beide Zellen eines Vergleichs sollen nebeneinander sortieren."""
    from testprompt import RUNS_DIR

    path = frontier.bare_log_path("cycleway-length")
    assert path.parent == RUNS_DIR
    assert path.name.endswith("__cycleway-length.frontier.jsonl")
