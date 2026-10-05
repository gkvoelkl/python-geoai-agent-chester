"""The prompt must not name a tool that does not exist.

Measured 2026-09-07 (`swiss-terrain-slope-grindelwald`, QGIS switched off): the agent
did not call a single QGIS tool — it could not, the catalogue held none. But the
**system prompt** named some 29 times: `qgis_clip`, `qgis_reproject`, `qgis_show`,
`qgis_run("native:joinattributestable")`, `qgis_service_area`. Its own snippet then read
"Since I can use qgis_process or just run it via GDAL in python", and the run lost 16
minutes before it found the existing `slope`.

For the measurement against a frontier model this is serious: the toolbox is presented
worse than it is. So a check instead of a request — it holds for **both** modes, because
it asks the catalogue rather than maintaining a list.

Deliberately only the QGIS family and not "every name mentioned must be a tool": the
general form caught `read_vector`/`write_vector` (namespace functions, deliberately not
tools) and the parameter `wfs_url`. A check with false alarms gets switched off, not
obeyed.
"""

from __future__ import annotations

import re

import pytest

import agent_build
from chester.qgis_env import qgis_disabled

#: Names that could be in the catalogue when QGIS is there — and have no business in the
#: prompt when it is missing. Algorithm ids too: they run only through `qgis_run`, which is
#: then missing as well.
_QGIS_MENTION = re.compile(r"\bqgis_[a-z0-9_]+|\b(?:native|grass|gdal|qgis):[a-z0-9_]+")


#: An instruction that raises while being built is **not** skipped. That is exactly what
#: this test's first draft did — and so hid the error it had caused itself:
#: `_INSTRUCTIONS.format(...)` over a text with literal curly braces (`{"building": "yes"}`
#: in the OSM examples) raises `KeyError('"building"')`. A skipped candidate is not a
#: passed one.
def _instruction_texts() -> dict[str, str]:
    out, broken = {}, {}
    for cap in agent_build.geo_capabilities():
        fn = cap.get_instructions()
        if fn is None:
            continue
        if not callable(fn):  # SelmaKit sometimes returns the finished text
            out[type(cap).__name__] = str(fn)
            continue
        try:
            text = fn(None)
        except Exception as exc:  # noqa: BLE001 - the error IS the finding
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
