"""Gate check: "claimed but never produced" — the answer names an output file that
does not exist on disk (e.g. the agent says it saved a .gpkg but no tool wrote it).

Advisory (a note, never a retry). Runs even when the run produced nothing at all,
so the phantom-file case is caught. Offline, no model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai.messages import ModelRequest, ToolReturnPart

from chester.gate import _absent_claims, make_validation_gate


def _good_gpkg(path: Path) -> Path:
    import geopandas as gpd
    from shapely.geometry import Point

    gpd.GeoDataFrame({"x": [1]}, geometry=[Point(11.0, 49.0)], crs="EPSG:25832").to_file(
        path, driver="GPKG"
    )
    return path


# ── the pure extractor ───────────────────────────────────────────────────────


def test_absent_claims_flags_missing_output_file(tmp_path):
    out = _absent_claims("Ich habe das Ergebnis in buildings.gpkg gespeichert.", str(tmp_path))
    assert out == ["buildings.gpkg"]


def test_absent_claims_silent_when_file_exists(tmp_path):
    (tmp_path / "geocache").mkdir()
    _good_gpkg(tmp_path / "geocache" / "buildings.gpkg")
    assert _absent_claims("Result saved to buildings.gpkg.", str(tmp_path)) == []


def test_absent_claims_ignores_urls_and_non_output_ext(tmp_path):
    text = "See https://example.org/data.tif and the source layer roads.shp."
    # .tif is behind a URL (skipped); .shp is not an output extension (not scanned)
    assert _absent_claims(text, str(tmp_path)) == []


def test_absent_claims_dedupes(tmp_path):
    out = _absent_claims("map.html here and again map.html there", str(tmp_path))
    assert out == ["map.html"]


def test_a_corrected_link_clears_the_earlier_mangled_one(tmp_path):
    """Eine Datei fehlt erst, wenn **keine** Nennung auf sie zeigt.

    Gemessen 2026-09-05 (`street-buildings-then-refine`, Schritt 1): Das Modell
    schrieb den Link zuerst als `(_Users/…/lappersdorf_map.html)` — Unterstrich statt
    führendem Schrägstrich —, das Gate meldete es, und die korrigierte absolute
    Angabe kam in derselben Antwort hinterher. Die alte Abkürzung über `seen`
    beurteilte die erste Schreibweise und übersprang jede weitere: gemeldet wurde
    „nicht vorhanden" über eine Datei, die existierte und drei Zeilen tiefer richtig
    verlinkt war.
    """
    (tmp_path / "geocache").mkdir()
    (tmp_path / "geocache" / "map.html").write_text("<html></html>", encoding="utf-8")
    text = ("Karte: [x](_Users/wrong/map.html)\n\n"
            f"Hier ist die Karte: [map.html]({tmp_path}/geocache/map.html)")
    assert _absent_claims(text, str(tmp_path)) == []


def test_a_file_named_only_wrongly_is_still_absent(tmp_path):
    """Die Lockerung darf den Phantomfall nicht mit durchlassen."""
    text = "Karte: [x](_Users/wrong/map.html) und nochmal (_Users/other/map.html)"
    assert _absent_claims(text, str(tmp_path)) == ["map.html"]


# ── gate integration ─────────────────────────────────────────────────────────


def _ctx(tool_output):
    part = ToolReturnPart(tool_name="fetch", content=tool_output, tool_call_id="c1")
    req = ModelRequest(parts=[part])
    run_id = None
    try:
        req.run_id = "R1"
        run_id = "R1"
    except Exception:  # noqa: BLE001
        pass
    return SimpleNamespace(deps="s1", messages=[req], run_id=run_id, retry=0, max_retries=1)


def _make(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "geocache").mkdir(parents=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    return make_validation_gate(sessions_dir=str(sessions), workspace=str(ws)), ws


def test_gate_notes_phantom_file_even_with_no_output(tmp_path):
    """The buildings-to-geopackage case: the run wrote nothing, the answer claims a
    .gpkg — the gate must still note it (runs before the no-paths early return)."""
    gate, _ws = _make(tmp_path)
    out = asyncio.run(
        gate(_ctx({"ok": True}), "Ich habe die Gebäude als buildings_4326.gpkg exportiert.")
    )
    assert "Validation note" in out and "buildings_4326.gpkg" in out


def test_gate_silent_when_claimed_file_exists(tmp_path):
    gate, ws = _make(tmp_path)
    _good_gpkg(ws / "geocache" / "buildings_4326.gpkg")
    answer = "Ich habe die Gebäude als buildings_4326.gpkg exportiert."
    produced = {"ok": True, "output": str(ws / "geocache" / "buildings_4326.gpkg")}
    out = asyncio.run(gate(_ctx(produced), answer))
    assert out == answer  # produced + exists → no note


def _retry_ctx(retry, max_retries):
    """Nur die zwei Felder, die die Retry-Arithmetik liest."""
    return SimpleNamespace(retry=retry, max_retries=max_retries)


def test_the_mild_defect_gets_its_own_retry_when_the_budget_allows():
    """Ein Antwortmangel darf nicht am schweren Mangel verhungern.

    Gemessen 2026-09-05 (`supermarket-accessibility-choropleth`): Der Ausdehnungs-
    Tier feuerte, der Agent clippte und rechnete neu (aus 18 Supermärkten wurden die
    richtigen 80) — und als der tote Link an die Reihe kam, war das Budget weg. Ein
    Lauf mit zwei Mängeln ist per Konstruktion genau der Lauf, in dem der milde
    verhungert. Der zweite Retry ist hier ungefährlich, weil die Behebung **keinen
    Werkzeugaufruf** kostet: dieselbe Antwort noch einmal, mit eingesetztem Pfad.
    """
    from chester.gate import _may_retry, _may_retry_answer_only

    # Budget 2 (SelmaKit mit retries={"tools": 4, "output": 2}):
    assert _may_retry(_retry_ctx(0, 2)) and _may_retry_answer_only(_retry_ctx(0, 2))
    assert not _may_retry(_retry_ctx(1, 2)), "der schwere Tier bleibt einmalig"
    assert _may_retry_answer_only(_retry_ctx(1, 2)), "der milde bekommt den zweiten"
    assert not _may_retry_answer_only(_retry_ctx(2, 2)), "und dann ist Schluss"


def test_the_second_retry_stays_inert_until_selmakit_raises_the_budget():
    """Bis SelmaKit `output: 2` übergibt, verhält sich alles wie bisher.

    Wichtig für die Reihenfolge der Auslieferung: Diese Chester-Seite darf allein
    ausgeliefert werden, ohne irgendein Verhalten zu ändern.
    """
    from chester.gate import _may_retry, _may_retry_answer_only

    for retry in (0, 1, 2):
        assert _may_retry_answer_only(_retry_ctx(retry, 1)) == _may_retry(_retry_ctx(retry, 1))
