"""Ratchet: German prose in code comments and docstrings may fall, never rise.

The language rule (CLAUDE.md, *Conventions*) puts every comment and docstring in
English. It was enforced by review only, and review does not run on every change:
between 2026-09-06 and 2026-09-14 six modules were written with German docstrings,
and `chester/mcpserver.py` with German identifiers. Measured on 2026-09-19: 6156
German function words across 132 files, most of them in `tests/`.

Converting all of that at once is not the point. The point is that the number stops
growing: each file is held to its count in `language_baseline.json`, and a file that
is not listed there must stay essentially free of German prose. The measure is a
count of German function words that are not also English words — a proxy, crude on
purpose. It needs no language model and it does not flag a German place name or a
quoted German user prompt, both of which are legitimate in code.

Scope is comments and docstrings only. User-facing string literals are out of scope
here: some tools (`check.sh`, the harness dashboard) talk to a German reader.

Update after a conversion (the new, lower value then shows up in the diff):
    uv run python tests/test_language.py --update-baseline
"""

from __future__ import annotations

import ast
import io
import json
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).parent / "language_baseline.json"

# German function words that are not English words ("die", "an", "in", "so", "was"
# are left out for that reason). Nouns would catch more and misfire on place names.
_GERMAN = frozenset(
    "und nicht ist wird werden eine einen einem einer eines der das dem den des mit "
    "für auf sich auch wenn oder noch nur kein keine keinen sind bei aus wie zu im "
    "vom zum zur über nach dass weil ohne jede jeder jedes wurde schon hier dort "
    "genau".split()
)
_WORD = re.compile(r"[A-Za-zÄÖÜäöüß]+")
# A new file may quote a German prompt or name a German source; beyond that it is prose.
_NEW_FILE_ALLOWANCE = 5
_SKIP = (".venv", "cache", ".chester", "harenessa", "internal", "postgis_test_db",
         "build", "mutants", ".mutmut-cache")


def _prose(source: str) -> str:
    """All comments and docstrings of one module, joined."""
    parts = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT
    ]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                parts.append(doc)
    return " ".join(parts)


#: Inline code spans. `fetch_dem`, `dem_path` and friends are identifiers, not prose —
#: counting them made every English docstring about terrain look German (2026-09-22).
_CODE_SPAN = re.compile(r"`[^`]*`")


def _is_german_word(word: str) -> bool:
    """Case matters for exactly one collision, and it is a frequent one.

    `DEM` — digital elevation model — is written upper case throughout this repo and
    lower-cased into the German article `dem`. Before this, a docstring reading "Slope
    in degrees from a DEM whose units are metres" counted as six words of German.
    A German sentence starting with "Dem" is now missed; that costs one word out of a
    forty-word set and is the cheaper error.
    """
    return word in _GERMAN if word.islower() else False


def german_prose_counts() -> dict[str, int]:
    out = {}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(_SKIP):
            continue
        text = _CODE_SPAN.sub(" ", _prose(path.read_text(encoding="utf-8", errors="replace")))
        n = sum(1 for w in _WORD.findall(text) if _is_german_word(w))
        if n:
            out[rel] = n
    return out


def _baseline() -> dict[str, int]:
    return json.loads(BASELINE.read_text()) if BASELINE.exists() else {}


def test_german_prose_does_not_grow():
    baseline = _baseline()
    counts = german_prose_counts()
    grown = {f: (n, baseline[f]) for f, n in counts.items() if f in baseline and n > baseline[f]}
    new = {f: n for f, n in counts.items() if f not in baseline and n > _NEW_FILE_ALLOWANCE}
    assert not grown and not new, (
        f"German prose in code grew {grown or ''} {new or ''} (now, baseline). Comments "
        "and docstrings are English (CLAUDE.md, Conventions) — translate them; do not "
        "raise the baseline."
    )


def test_the_marker_words_are_not_english():
    # Guards the proxy itself: an English word in the set would count English prose.
    assert not _GERMAN & {"die", "an", "in", "so", "was", "also", "man", "will", "bin"}


def test_the_proxy_separates_the_two_languages():
    german = '"""Liest die Datei und gibt nicht mehr zurück, als der Aufrufer braucht."""\n'
    english = '"""Reads the file and returns no more than the caller needs."""\n'
    place = '"""Fetch boundaries for Landkreis Regensburg ("Einwohner je km²")."""\n'
    count = lambda src: sum(w.lower() in _GERMAN for w in _WORD.findall(_prose(src)))  # noqa: E731
    assert count(german) >= 3
    assert count(english) == 0
    assert count(place) == 0


if __name__ == "__main__" and "--update-baseline" in sys.argv:
    current = german_prose_counts()
    raised = {f: (n, _baseline()[f]) for f, n in current.items()
              if f in _baseline() and n > _baseline()[f]}
    # A file with no entry has no German to protect, so it starts at the allowance —
    # the same rule `test_german_prose_does_not_grow` applies. Without this the updater
    # writes the first German of an untouched file into the baseline and legitimises
    # exactly what the test would have refused (found 2026-09-21, the third ratchet
    # that day with the same hole: lint and mypy had it too).
    raised |= {f: (n, 0) for f, n in current.items()
               if f not in _baseline() and n > _NEW_FILE_ALLOWANCE}
    if raised:
        sys.exit(f"refusing: counts rose {raised} — translate instead of raising")
    BASELINE.write_text(json.dumps(dict(sorted(current.items())), indent=1) + "\n")
    print(f"language baseline written: {sum(current.values())} words in {len(current)} files")


def test_the_guards_substitution_table_is_english():
    """The text a guard sends *into a model prompt* is English — no baseline, no growth.

    `_HAND_ROLLED` pairs a hand-rolled operation with the checked tool that replaces it
    and what that tool adds. `runtime.geopython` assembles those halves into its
    refusal, so the model reads them. Ten of them were German until 2026-09-26, when a
    Test-Level-3 run made the mixed sentence visible in the trace: "a checked function
    already does this: `vector_reproject` — meldet Objektzahl und Ziel-CRS zurück".

    The ratchet above cannot catch this: its scope is comments and docstrings, because
    some literals legitimately address a German reader (`check.sh`, the dashboard). That
    exemption cannot tell a human reader from a model one — so the prompt-facing tables
    get their own check, and this one is absolute rather than a count.
    """
    from chester.geo_python import _HAND_ROLLED

    offenders = {}
    for _raw, name, _checked, gain in _HAND_ROLLED:
        german = sorted({w for w in _WORD.findall(gain) if _is_german_word(w)})
        if german:
            offenders[name] = german
    assert not offenders, (
        f"German prose in a guard message the model reads: {offenders}. "
        "Prompt-injected text is English (CLAUDE.md, Conventions) — translate it.")
