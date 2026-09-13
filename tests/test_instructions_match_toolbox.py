"""Der Prompt darf kein Werkzeug nennen, das es nicht gibt.

Gemessen 2026-09-07 (`swiss-terrain-slope-grindelwald`, QGIS abgeschaltet): Der Agent
rief kein einziges QGIS-Werkzeug auf — er konnte nicht, der Katalog hielt keins. Der
**System-Prompt** nannte aber 29-mal welche: `qgis_clip`, `qgis_reproject`,
`qgis_show`, `qgis_run("native:joinattributestable")`, `qgis_service_area`. In seinem
eigenen Schnipsel stand daraufhin „Since I can use qgis_process or just run it via
GDAL in python", und der Lauf verlor 16 Minuten, bevor er das vorhandene `slope` fand.

Für die Messung gegen ein Frontier-Modell ist das gravierend: Der Werkzeugkasten wird
schlechter dargestellt, als er ist. Also eine Prüfung statt einer Bitte — sie hält für
**beide** Betriebsarten, weil sie den Katalog fragt und nicht eine Liste pflegt.

Bewusst nur die QGIS-Familie und nicht „jeder genannte Name muss ein Werkzeug sein":
Die allgemeine Form fing `read_vector`/`write_vector` (Namensraum-Funktionen, absichtlich
keine Werkzeuge) und den Parameter `wfs_url`. Eine Prüfung mit Fehlalarmen wird
abgeschaltet, nicht befolgt.
"""

from __future__ import annotations

import re

import pytest

import agent_build
from chester.qgis_env import qgis_disabled

#: Namen, die im Katalog stehen könnten, wenn QGIS da ist — und im Prompt nichts zu
#: suchen haben, wenn es fehlt. Auch Algorithmus-Kennungen: die laufen nur über
#: `qgis_run`, das dann ebenfalls fehlt.
_QGIS_MENTION = re.compile(r"\bqgis_[a-z0-9_]+|\b(?:native|grass|gdal|qgis):[a-z0-9_]+")


#: Eine Instruktion, die beim Bauen wirft, wird **nicht** übersprungen. Genau das
#: hat der erste Entwurf dieses Tests getan — und dabei den Fehler verdeckt, den er
#: selbst verursacht hatte: `_INSTRUCTIONS.format(...)` über einen Text mit literalen
#: geschweiften Klammern (`{"building": "yes"}` in den OSM-Beispielen) wirft
#: `KeyError('"building"')`. Ein übersprungener Prüfling ist kein bestandener.
def _instruction_texts() -> dict[str, str]:
    out, broken = {}, {}
    for cap in agent_build.geo_capabilities():
        fn = cap.get_instructions()
        if fn is None:
            continue
        if not callable(fn):  # SelmaKit liefert teils den fertigen Text
            out[type(cap).__name__] = str(fn)
            continue
        try:
            text = fn(None)
        except Exception as exc:  # noqa: BLE001 - der Fehler IST der Befund
            broken[type(cap).__name__] = f"{type(exc).__name__}: {exc}"
            continue
        if text:
            out[type(cap).__name__] = text
    assert not broken, f"Instruktion lässt sich nicht bauen: {broken}"
    return out


def _tool_names() -> set[str]:
    names = set()
    for cap in agent_build.geo_capabilities():
        toolset = cap.get_toolset()
        if toolset is not None:
            names |= set(getattr(toolset, "tools", {}) or {})
    return names


@pytest.mark.skipif(not qgis_disabled(), reason="nur ohne QGIS aussagekräftig")
def test_no_instruction_names_a_qgis_tool_when_qgis_is_off():
    offenders = {}
    for name, text in _instruction_texts().items():
        hits = sorted(set(_QGIS_MENTION.findall(text)))
        hits = [h for h in hits if h]
        if hits:
            offenders[name] = hits
    assert not offenders, (
        "Der Prompt nennt Werkzeuge, die der Agent ohne QGIS nicht hat: "
        f"{offenders}. Ohne QGIS gehören dort die geprüften Entsprechungen hin "
        "(`vector_clip`, `vector_reproject`, `vector_join`, `service_area` …); "
        "reine QGIS-Desktop-Sätze (`qgis_show*`) gehören ganz weg.")
