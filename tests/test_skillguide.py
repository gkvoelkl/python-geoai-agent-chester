"""Die Skill-Führung im Prompt — eine Regel, keine zweite Liste.

Anlass (2026-09-03): Über **108** Sitzungen gab es zwei `load_capability`-Aufrufe,
beide auf namentliche Aufforderung. Der Katalog *war* im Prompt, aber siebzig Zeilen
von der Auswahlregel entfernt und unter der Zeile „A capability's tools stay hidden
until it is loaded" — die für Chesters Skills schlicht falsch ist, weil sie keine
Werkzeuge tragen.

Der erste Versuch war, die Liste neben der Regel zu wiederholen. Er half messbar
nicht (der Lauf danach verhielt sich zeichengleich zu den drei davor) und war eine
echte Dublette: `Skills._to_capability` im Harness baut die Katalogeinträge aus
demselben Front Matter. Diese Tests halten deshalb die *jetzige* Eigenschaft fest —
die Beschreibungen stehen genau einmal im Prompt, und zwar dort, wo das Framework
sie ohnehin rendert.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from chester.capabilities.skillguide import GeoSkillGuideCapability


def _rendered() -> str:
    cap = GeoSkillGuideCapability()
    return cap.get_instructions()(SimpleNamespace(deps=None))


def test_the_rule_survives():
    """Ohne Auswahlregel bleibt nur der irreführende Framework-Satz stehen."""
    text = _rendered()
    assert "exactly one clearly fits" in text
    assert "load_capability" in text
    assert "At most one skill per turn" in text


def test_it_corrects_the_frameworks_claim_about_hidden_tools():
    """Der Grund, warum diese Capability überhaupt existiert.

    pydantic-ai schreibt über den Katalog „A capability's tools stay hidden until it
    is loaded". Für Chesters Skills ist das falsch — sie tragen keine Werkzeuge, und
    ein Modell, das den Satz glaubt, hat keinen Anlass zu laden.

    Geprüft gegen die **Konstante des Frameworks**, nicht gegen eine abgeschriebene
    Fassung: Formuliert pydantic-ai den Satz um, zeigt die Richtigstellung ins Leere,
    und genau dann soll dieser Test anschlagen.
    """
    from pydantic_ai.capabilities._deferred_capability_loader import (
        DEFERRED_CAPABILITY_CATALOG_PREFIX,
    )

    quoted = "tools stay hidden until it is loaded"
    assert quoted in " ".join(DEFERRED_CAPABILITY_CATALOG_PREFIX.split()), (
        "pydantic-ai hat den Katalogsatz geändert — die Richtigstellung nachziehen"
    )
    text = " ".join(_rendered().split())  # Prompt ist umbrochen, der Satz nicht
    assert "no hidden tools" in text
    assert quoted in text  # zitiert, um es zu widerlegen


def test_no_second_copy_of_the_skill_descriptions():
    """Die Dublette, die hier einen Tag lang stand: 2.594 Zeichen für denselben Text.

    Der Framework-Katalog rendert Name und Beschreibung aus dem Front Matter. Sie
    hier zu wiederholen kostet 5,7 % des Prompts und ändert am Verhalten nichts.
    """
    text = _rendered()
    for skill in Path("skills").glob("*/SKILL.md"):
        head = skill.read_text(encoding="utf-8").split("---", 2)
        meta = head[1] if len(head) > 2 else ""
        desc = next(
            (x.split(":", 1)[1].strip().strip("\"'") for x in meta.splitlines()
             if x.startswith("description:")),
            None,
        )
        assert desc, f"{skill} ohne description im Front Matter"
        assert desc not in text, f"Beschreibung von {skill.parent.name} doppelt im Prompt"


def test_only_guidance_never_the_recipes():
    """Die Rezepte bleiben draußen: +10.553 Token wären +86 % auf den Prompt."""
    text = _rendered()
    assert "## Steps" not in text and "## Inputs" not in text
    assert len(text) < 1500, f"Instruktionsblock auf {len(text)} Zeichen gewachsen"
