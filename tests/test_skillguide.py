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

from chester.runtime.skillguide import GeoSkillGuideCapability

#: The skills as they are shipped in the repo — `setup.py` copies them into the
#: workspace, so the repo copy is the one a test may rely on being there.
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


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


def test_every_tool_a_skill_names_actually_exists():
    """A recipe that names a tool the reader does not have is worse than no recipe.

    Measured 2026-09-21: the nine skills named `qgis_*` tools **36 times** while this
    installation runs with `use_qgis: false`, and `find-official-data` told the agent
    to call `delegate_task` although SelmaKit's subagents extra is not installed. The
    cost showed up in a team run — a ressort read a skill and then called tools from
    it that do not exist, four times, until its time limit stopped it. `terrain-analysis`
    was the clearest case: it explained `qgis_run("native:slope")` while `slope` had
    been a tool of the toolbox all along.

    Agent-level tools are allowed by name because they live outside the wrapper layer;
    every other backticked call must be a real one.
    """
    import re

    from chester import wrapperlayer

    real = {t.__name__ for t in wrapperlayer.collect_tools("/tmp/chester-skill-probe")}
    # Outside the wrapper layer, but present for the single agent and (some) ressorts.
    agent_level = {"geo_python_run", "inspect_map", "web_search", "web_fetch",
                   "load_capability", "write_plan", "read_tool_result"}
    unknown: dict[str, list[str]] = {}
    for skill in sorted(SKILLS_DIR.iterdir()):
        doc = skill / "SKILL.md"
        if not doc.is_file():
            continue
        named = set(re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\(", doc.read_text()))
        missing = sorted(n for n in named if n not in real and n not in agent_level)
        if missing:
            unknown[skill.name] = missing
    assert not unknown, (
        f"Skills nennen Werkzeuge, die es nicht gibt: {unknown}. Das Rezept auf ein "
        "vorhandenes Werkzeug umschreiben — oder ehrlich sagen, dass der Schritt hier "
        "nicht geht."
    )


def test_no_skill_depends_on_qgis_being_installed():
    """QGIS is optional (`geodata.use_qgis`), so a skill that *routes through* it is
    unusable in half the configurations — including the one the benchmarks run in.
    Naming the `qgis_*` family at all is the tell: the wrapper layer has a twin for
    almost every algorithm these skills used (`slope`, `vector_clip`, `zonal_stats`, …).
    Where there is genuinely no twin — PDAL ground classification — the skill says so
    instead of calling a tool that may not be there (`lidar-ground`).
    """
    offenders = {s.name: [ln.strip() for ln in (s / "SKILL.md").read_text().splitlines()
                          if "qgis_" in ln]
                 for s in sorted(SKILLS_DIR.iterdir()) if (s / "SKILL.md").is_file()}
    offenders = {k: v for k, v in offenders.items() if v}
    assert not offenders, f"Skills mit QGIS-Abhängigkeit: {offenders}"
