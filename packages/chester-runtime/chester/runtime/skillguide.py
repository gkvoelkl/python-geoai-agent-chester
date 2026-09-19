"""GeoSkillGuideCapability — tells the model *when* to reach for a skill.

Instructions only, no tools. It exists because of what selmakit 0.1.26 changed:
skills used to arrive as an ``<available_skills>`` block with hand-written selection
rules ("scan the descriptions; if exactly one clearly applies, load it; never more
than one per turn"). They now arrive as **deferred capabilities**, and those rules
were dropped along the way — pydantic-ai renders the catalogue with one generic
line:

    The following capabilities are deferred and can be loaded using the
    `load_capability` tool. A capability's tools stay hidden until it is loaded.

For Chester that sentence is actively misleading. Its skills carry **no tools** —
they are workflow recipes, and every geo tool is already on the table. A model
reading "loading reveals hidden tools" sees no reason to load anything, because it
is not missing any tools.

Measured before this capability existed: across 65 persisted sessions there were
**two** `load_capability` calls, both from runs where a skill was demanded by name.
Not one skill ever loaded itself — including a run that wandered into hand-written
PyQGIS instead of fetching a city boundary, which is exactly what
``find-official-data`` describes.

Kept deliberately short. This text is in *every* prompt while a skill body is only
pulled when needed, so the guidance must not cost more than the deferral saves.

**Nachtrag 2026-09-03 — die Liste ist wieder raus, die Regel bleibt.** Sie stand
hier einen Tag lang, weil der Framework-Katalog siebzig Zeilen entfernt unter dem
für Chester falschen Satz „A capability's tools stay hidden until it is loaded"
gerendert wird. Zwei Messungen haben das erledigt.

*Sie half nicht.* Der erste Lauf mit der Liste im Prompt
(`pluvial-flow-accumulation-tegernheim`, 03.09.) verhielt sich zeichengleich zu den
drei Läufen davor ohne sie: `load_capability` blieb bei 0, und das Modell suchte wie
zuvor zuerst nach `contour`. Eine Regel gewinnt nicht dadurch, dass man sie lauter
wiederholt, wenn sie sechzig Zeilen hinter ihrer Gegenaussage steht.

*Und sie war eine echte Dublette.* `Skills._to_capability` im Harness baut
``Capability(id=skill.name, description=skill.description)`` direkt aus dem Front
Matter — der Framework-Katalog **ist** genau diese Liste. Die Wiederholung kostete
2.594 Zeichen, 5,7 % des Prompts, für denselben Text an zweiter Stelle.

Was bleibt, ist das, was das Framework nicht liefert: die Auswahlregel und die
Richtigstellung, dass diese Capabilities keine Werkzeuge verstecken. Sie verweist
auf den Katalog, statt ihn zu kopieren. Die Rezepte selbst bleiben ohnehin draußen —
sie wiegen zusammen 10.553 Token gegen 12.327 des ganzen Prompts (+86 %).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability

_INSTRUCTIONS = """\
## Skills (deferred capabilities)

**Correction to the catalogue above:** those deferred capabilities hold **no hidden
tools** — every geo tool is already available to you, so "tools stay hidden until it
is loaded" does not apply to them. What each one adds is the *route*: which source to
prefer, in which order, and the traps in between.

Before starting a multi-step geo task, scan that catalogue's descriptions:
- exactly one clearly fits → load it with `load_capability` and follow it;
- several could fit → take the most specific one;
- none clearly fits → load nothing and work directly.

At most one skill per turn, and only after choosing — loading is for following, not
for browsing.
"""


@dataclass
class GeoSkillGuideCapability(AbstractCapability[Any]):
    """Restores the skill-selection rule that the 0.1.26 catalogue no longer carries."""

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return _INSTRUCTIONS

        return _instructions
