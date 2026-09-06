"""Die Protokoll-Capability — ein Beobachter, der den Lauf nicht verändern darf.

Anlass (2026-09-04): Ein Dialogzug wechselte nach einer Nutzerbeschwerde auf die
richtige Methode, rechnete `grass:r.watershed`, schrieb eine Karte — und starb dann
am Anfragelimit. SelmaKit schreibt die Sitzung am *Ende* eines Zuges, also existierte
von alldem hinterher kein Protokoll. Diese Capability hängt jede Zeile sofort an.

Zwei Eigenschaften sind hier wichtiger als der Inhalt: Die Haken sind
Durchreichungen, und der Fehlerhaken **wirft weiter**. Sein Kontrakt lautet „return
any value to suppress the error and use it as the tool result" — ein `return None`
hätte jeden Werkzeugfehler still verschluckt.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from chester.capabilities.runlog import RunLogCapability, _safe_name, _short


def _ctx(key="s1"):
    return SimpleNamespace(deps=key)


def _call(cid="c1"):
    return SimpleNamespace(tool_call_id=cid)


def _tool(name="geocode"):
    return SimpleNamespace(name=name)


def _lines(tmp_path, key="s1"):
    path = tmp_path / f"{key}.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_a_call_is_written_before_the_tool_runs(tmp_path):
    """Der Sinn: Ein hängendes Werkzeug ist als solches sichtbar."""
    cap = RunLogCapability(log_dir=str(tmp_path))
    asyncio.run(cap.before_tool_execute(_ctx(), call=_call(), tool_def=_tool(), args={"q": "x"}))
    entries = _lines(tmp_path)
    assert [e["kind"] for e in entries] == ["call"]
    assert entries[0]["tool"] == "geocode"


def test_hooks_pass_their_payload_through_unchanged(tmp_path):
    """Ein Beobachter, der Argumente oder Ergebnisse verändert, ist keiner."""
    cap = RunLogCapability(log_dir=str(tmp_path))
    args = {"q": "x"}
    result = {"ok": True, "n": 3}
    assert asyncio.run(
        cap.before_tool_execute(_ctx(), call=_call(), tool_def=_tool(), args=args)
    ) is args
    assert asyncio.run(
        cap.after_tool_execute(_ctx(), call=_call(), tool_def=_tool(), args=args, result=result)
    ) is result


def test_a_tool_error_is_logged_and_re_raised(tmp_path):
    """Die gefährliche Stelle: Rückgabe statt Wurf würde den Fehler unterdrücken."""
    cap = RunLogCapability(log_dir=str(tmp_path))
    boom = ValueError("kaputt")
    with pytest.raises(ValueError, match="kaputt"):
        asyncio.run(
            cap.on_tool_execute_error(
                _ctx(), call=_call(), tool_def=_tool(), args={}, error=boom
            )
        )
    entry = _lines(tmp_path)[-1]
    assert entry["kind"] == "error"
    assert "ValueError: kaputt" in entry["error"]


def test_the_elapsed_time_pairs_call_and_result(tmp_path):
    cap = RunLogCapability(log_dir=str(tmp_path))
    asyncio.run(cap.before_tool_execute(_ctx(), call=_call("abc"), tool_def=_tool(), args={}))
    asyncio.run(
        cap.after_tool_execute(_ctx(), call=_call("abc"), tool_def=_tool(), args={}, result={})
    )
    assert _lines(tmp_path)[-1]["seconds"] is not None


def test_an_unwritable_directory_does_not_break_the_run(tmp_path):
    """Ein Beobachter, der den beobachteten Lauf fällen kann, ist schlimmer als keiner."""
    cap = RunLogCapability(log_dir="/proc/nope/definitely-not-writable")
    args = {"q": "x"}
    assert asyncio.run(
        cap.before_tool_execute(_ctx(), call=_call(), tool_def=_tool(), args=args)
    ) is args


def test_long_values_are_truncated_but_their_size_is_kept():
    """Ein `vector_info` auf einer OSM-Ebene würde das Protokoll sonst zuschütten."""
    out = _short("x" * 5000)
    assert len(out) < 1400
    assert "+3800 chars" in out


def test_session_keys_with_separators_become_one_filename():
    assert _safe_name("testprompt:pluvial-flow") == "testprompt_pluvial-flow"
    assert "/" not in _safe_name("a/b")


def test_the_model_reply_is_recorded_with_a_repetition_count(tmp_path):
    """Der dritte Textausfall des 2026-09-04 blieb unbelegt: Der Zug hatte eine
    Karte fertig und wiederholte danach den Link endlos. Werkzeugaufrufe zeigen das
    nicht — es waren 21, alle erfolgreich —, und der Abbruch verhinderte, dass die
    Sitzung geschrieben wurde. `repeats` macht die Entartung zur Zahl."""
    cap = RunLogCapability(log_dir=str(tmp_path))
    resp = SimpleNamespace(
        parts=[SimpleNamespace(part_kind="text", content="Hier: /a.html\n" * 40)]
    )
    out = asyncio.run(cap.after_model_request(_ctx(), request_context=None, response=resp))
    assert out is resp, "die Antwort muss unverändert durchgereicht werden"
    entry = _lines(tmp_path)[-1]
    assert entry["kind"] == "text"
    assert entry["repeats"] == 40


def test_a_healthy_reply_has_a_low_repetition_count(tmp_path):
    cap = RunLogCapability(log_dir=str(tmp_path))
    resp = SimpleNamespace(
        parts=[SimpleNamespace(part_kind="text", content="Die Fläche beträgt 11,54 km².")]
    )
    asyncio.run(cap.after_model_request(_ctx(), request_context=None, response=resp))
    assert _lines(tmp_path)[-1]["repeats"] == 1


def test_a_reply_without_text_writes_nothing(tmp_path):
    """Eine reine Werkzeugantwort ist kein Text — sonst stünde je Zug eine Leerzeile."""
    cap = RunLogCapability(log_dir=str(tmp_path))
    resp = SimpleNamespace(parts=[SimpleNamespace(part_kind="tool-call", content=None)])
    asyncio.run(cap.after_model_request(_ctx(), request_context=None, response=resp))
    assert _lines(tmp_path) == []


def test_a_call_that_fails_validation_is_recorded_and_re_raised(tmp_path):
    """Die blinde Stelle, die am 2026-09-05 ein ganzes Protokoll kostete.

    `before_tool_execute` feuert erst **nach** der Argumentprüfung. Der erste
    Aufruf des Probenlaufs war `write_plan` mit `"id": 1`, wo `PlanItem.id` eine
    Zeichenkette verlangt — abgewiesen vor der Ausführung, also kein Haken, also
    keine Datei. Von außen sah der Lauf aus, als hätte er kein Werkzeug benutzt;
    tatsächlich hatte er es versucht.
    """
    cap = RunLogCapability(log_dir=str(tmp_path))
    boom = ValueError("Input should be a valid string")
    with pytest.raises(ValueError, match="valid string"):
        asyncio.run(
            cap.on_tool_validate_error(
                _ctx(), call=_call(), tool_def=_tool("write_plan"),
                args={"items": [{"id": 1}]}, error=boom,
            )
        )
    entry = _lines(tmp_path)[-1]
    assert entry["kind"] == "invalid"
    assert entry["tool"] == "write_plan"
    assert "valid string" in entry["error"]


def test_the_validate_error_hook_must_not_swallow(tmp_path):
    """Gegenprobe zum Kontrakt: Ein Rückgabewert würde als *geprüfte* Argumente
    gelten und den fehlerhaften Aufruf ausführen — dieselbe Falle wie bei
    `on_tool_execute_error`."""
    import inspect

    src = inspect.getsource(RunLogCapability.on_tool_validate_error)
    assert "raise error" in src
