"""Administrative-level escalation over coded region keys (no deps).

When data for one small unit (a Gemeinde) isn't published on its own, it is almost
always part of a comprehensive dataset for a *containing* level (all Gemeinden of
the Kreis / Land / Bund) held by a more central authority. Those coded region
keys — the **AGS** (Amtlicher Gemeindeschlüssel) and **NUTS** — encode the
containment hierarchy as a **prefix**, so "escalate to the next-higher level" is
literally "shorten the prefix":

    09375117  Gemeinde Barbing
    09375     Landkreis Regensburg   (Land 09 · Regierungsbezirk 3 · Kreis 75)
    09        Bayern
    ""        Deutschland  (matches every AGS)

``region_hierarchy(code)`` turns a region code into that escalation chain, so the
agent can fetch the comprehensive dataset for a wider scope and filter it by the
prefix — **preserving the target granularity** (all Gemeinden), never substituting
a higher-level aggregate for a missing unit value.

AGS digit layout: Land(2) · Regierungsbezirk(1) · Kreis(2) · Gemeinde(3) = 8.
NUTS: country(2) · NUTS1(3) · NUTS2(4) · NUTS3(5).
"""

from __future__ import annotations

import re

# A NUTS code is a 2-letter country code plus up to 3 alphanumeric chars (NUTS 1–3).
_NUTS_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{0,3}")

_AGS_LEVEL_BY_LEN = {2: "Land", 3: "Regierungsbezirk", 5: "Kreis", 8: "Gemeinde",
                     12: "Gemeinde"}
# Scope lengths to widen to (from narrowest parent to Bund); 0 = the whole country.
_AGS_SCOPE_LENS = (5, 3, 2, 0)

_NUTS_LEVEL_BY_LEN = {2: "country (NUTS0)", 3: "NUTS1 (Bundesland)",
                      4: "NUTS2 (Regierungsbezirk)", 5: "NUTS3 (Kreis)"}
_NUTS_SCOPE_LENS = (4, 3, 2)

_NOTE = (
    "Escalate the search *scope*, not the target *granularity*: fetch the "
    "comprehensive dataset for a wider scope and filter by 'prefix' (e.g. "
    "stats_table('wikidata', prefix) for per-Gemeinde population, or a national "
    "boundary set clipped to your area). The input's own code is already the "
    "prefix that selects all sub-units within it. Never substitute a higher-level "
    "aggregate for a missing unit value — if no level carries the needed "
    "granularity, report the blocker instead of fabricating one."
)


def _hierarchy(code: str, level_by_len: dict, scope_lens, key: str) -> dict:
    length = len(code)
    escalation = [
        {"scope": level_by_len.get(n, "Bund" if n == 0 else f"len-{n}"),
         "code": code[:n], "prefix": code[:n]}
        for n in scope_lens if n < length
    ]
    return {
        "ok": True,
        "key": key,
        "input": {"code": code, "level": level_by_len.get(length, "unknown")},
        "escalation": escalation,
        "note": _NOTE,
    }


def region_hierarchy(code: str) -> dict:
    """The escalation chain for an AGS/Kreisschlüssel or NUTS ``code``.

    Returns each wider *scope* with the code prefix to fetch it. Digits → AGS
    (``"09375"`` → Kreis, ``"09"`` → Land, ``""`` → Bund); a leading two letters
    → NUTS (``"DE232"`` → NUTS-3, up to ``"DE"`` country).
    """
    code = (code or "").strip().upper()
    if not code:
        return {"ok": False, "error": "give an AGS/Kreisschlüssel (digits) or a NUTS code"}
    if code.isdigit() and len(code) <= 12:
        return _hierarchy(code, _AGS_LEVEL_BY_LEN, _AGS_SCOPE_LENS, "ags")
    if _NUTS_RE.fullmatch(code):
        return _hierarchy(code, _NUTS_LEVEL_BY_LEN, _NUTS_SCOPE_LENS, "nuts")
    return {"ok": False,
            "error": f"unrecognised region code {code!r} — digits for AGS "
            "(e.g. '09375'), a 2–5 char NUTS code for NUTS (e.g. 'DE23')"}
