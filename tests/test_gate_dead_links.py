"""Tote Linkziele in der Antwort — der Platzhalter-Fall.

Gemessen 2026-09-07 (`dop-aerial-regensburg`): Die Antwort endete mit
``[Regensburger Altstadt Luftbild](_the_absolute_path_from_the_tool_call_)``. Das
Luftbild war richtig, die Karte geschrieben — nur der Link zeigte ins Nichts. Der
Platzhalter steht **nirgends** in der Instruktion; die beschreibt den Pfad in Prosa
(„the exact `output` path that render_map returned"), und das Modell hat die
Beschreibung in die Klammer geschrieben. Über 75 Bank-Läufe viermal, in drei
Wortlauten (`_remote_path_to_map_`, `_path_to_map_file_`).

`_unquoted_view_paths` griff nicht, weil sein Auslöser `_mentioned` ist und der
Platzhalter weder Basisnamen noch Stamm nennt. Diese Tests halten den zweiten
Auslöser fest — und die Grenze, an der er schweigen muss.
"""

from __future__ import annotations

from chester.gate import _dead_link_targets, _unquoted_view_paths


def _map(tmp_path, name="regensburg_altstadt_map.html"):
    (tmp_path / "geocache").mkdir(parents=True, exist_ok=True)
    f = tmp_path / "geocache" / name
    f.write_text("<html>karte</html>", encoding="utf-8")
    return str(f)


def test_the_placeholder_from_the_run_is_caught(tmp_path):
    answer = ("Hier ist ein aktuelles Luftbild der Regensburger Altstadt:\n\n"
              "[Regensburger Altstadt Luftbild](_the_absolute_path_from_the_tool_call_)")
    assert _dead_link_targets(answer, str(tmp_path)) == [
        "_the_absolute_path_from_the_tool_call_"]


def test_the_other_two_wordings_are_caught_too(tmp_path):
    for target in ("_remote_path_to_map_", "_path_to_map_file_"):
        assert _dead_link_targets(f"[Karte]({target})", str(tmp_path)) == [target]


def test_a_missing_file_with_an_extension_is_caught(tmp_path):
    """Auch die Rutscher-Form: ein Unterstrich, wo der führende Schrägstrich hingehört."""
    answer = "[Karte](_Users/x/geocache/lappersdorf_map.html)"
    assert _dead_link_targets(answer, str(tmp_path)) == [
        "_Users/x/geocache/lappersdorf_map.html"]


def test_a_real_file_is_not_a_dead_link(tmp_path):
    path = _map(tmp_path)
    assert _dead_link_targets(f"[Karte]({path})", str(tmp_path)) == []


def test_web_links_are_left_alone(tmp_path):
    """Die Grenze: ohne Schema, aber eine Domain — kein Dateipfad, keine Meldung."""
    answer = ("Quelle: [OpenStreetMap](https://www.openstreetmap.org) und "
              "[Geodatenportal](www.geodaten.bayern.de) sowie [Abschnitt](#daten).")
    assert _dead_link_targets(answer, str(tmp_path)) == []


def test_the_gate_now_finds_the_view_behind_a_dead_link(tmp_path):
    """Der Zweck des Ganzen: Die gerenderte Karte wird als unerreichbar gemeldet."""
    path = _map(tmp_path)
    results = [("render_map", {"ok": True, "output": path})]
    answer = "Hier ist die Karte:\n\n[Luftbild](_the_absolute_path_from_the_tool_call_)"
    assert _unquoted_view_paths(results, answer, str(tmp_path)) == [path]


def test_a_quoted_path_stays_silent(tmp_path):
    """Kein Fehlalarm, wenn die Antwort den Pfad korrekt nennt."""
    path = _map(tmp_path)
    results = [("render_map", {"ok": True, "output": path})]
    assert _unquoted_view_paths(results, f"Die Karte: {path}", str(tmp_path)) == []


def test_an_unmentioned_intermediate_view_stays_silent(tmp_path):
    """Die alte Zusicherung darf nicht kippen: eine nie erwähnte Ansicht ist kein Defekt."""
    path = _map(tmp_path, "zwischenschritt.html")
    results = [("render_map", {"ok": True, "output": path})]
    answer = "Die Fläche beträgt 12,4 km². Details siehe [Quelle](https://osm.org)."
    assert _unquoted_view_paths(results, answer, str(tmp_path)) == []
