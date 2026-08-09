"""Tests for admin-level escalation (region_hierarchy) — pure, no network."""

from __future__ import annotations

from chester.adminlevels import region_hierarchy


def test_gemeinde_ags_escalates_to_bund():
    r = region_hierarchy("09375117")  # Gemeinde Barbing
    assert r["ok"] and r["key"] == "ags"
    assert r["input"]["level"] == "Gemeinde"
    scopes = [(e["scope"], e["prefix"]) for e in r["escalation"]]
    assert scopes == [
        ("Kreis", "09375"),
        ("Regierungsbezirk", "093"),
        ("Land", "09"),
        ("Bund", ""),
    ]


def test_kreis_ags_escalates_upward_only():
    r = region_hierarchy("09375")  # Landkreis Regensburg
    assert r["input"]["level"] == "Kreis"
    assert [e["prefix"] for e in r["escalation"]] == ["093", "09", ""]


def test_land_ags_escalates_to_bund_only():
    r = region_hierarchy("09")
    assert r["input"]["level"] == "Land"
    assert [e["scope"] for e in r["escalation"]] == ["Bund"]
    assert r["escalation"][0]["prefix"] == ""


def test_prefixes_are_strict_truncations():
    r = region_hierarchy("09375117")
    for e in r["escalation"]:
        assert "09375117".startswith(e["prefix"])


def test_nuts_code_escalates_to_country():
    r = region_hierarchy("DE232")  # a NUTS-3 code
    assert r["ok"] and r["key"] == "nuts"
    assert [e["prefix"] for e in r["escalation"]] == ["DE23", "DE2", "DE"]


def test_lowercase_nuts_is_normalised():
    r = region_hierarchy("de2")
    assert r["ok"] and r["key"] == "nuts"
    assert r["escalation"][0]["prefix"] == "DE"


def test_bad_and_empty_codes_error():
    assert region_hierarchy("")["ok"] is False
    assert region_hierarchy("BADCODE")["ok"] is False   # 7 letters: not a NUTS code
    assert region_hierarchy("12-3")["ok"] is False
