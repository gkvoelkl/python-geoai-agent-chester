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
