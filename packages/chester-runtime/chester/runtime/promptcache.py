"""PromptCacheCapability — pay for Chester's system prompt once, not once per step.

Chester re-sends a large, *stable* prefix on every single model call: roughly 14k
tokens of capability instructions plus the tool schemas of the whole toolbox. A
local model absorbs that in its own prefix cache (measured 2026-08-22: unchanged
prompt 0.1 s, one line changed mid-prompt 52.8 s). A **hosted** model bills it —
every step, at full input price.

Measured 2026-09-09 with `claude-opus-4-8`: a benchmark run is ~20 model calls,
so an uncached run pays the prefix ~20 times. At $5/MTok that is the difference
between roughly $5 and $1.40 for the *same* run — with a 10 € budget, between two
runs and seven.

Anthropic's prompt cache fixes exactly this, but pydantic-ai leaves it **opt-in**
(`anthropic_cache*` in `AnthropicModelSettings`), and nothing in Chester set it.
This capability sets it — and nothing else. It adds no instructions and no tools,
because anything it added to the prompt would be the thing it exists to make cheap.

**Provider-scoped on purpose.** The settings only go out when `model.model` names
the Anthropic provider. The KO series compares an ablated build against the
current one (`internal/TODO.md` KO.1), and that comparison is only worth
something if the local L+ cell is bit-identical to the build it stands in for.
A model setting that never leaves this method cannot change it.

TTLs differ by layer, and the reason is the write premium (5m costs 1.25x a
normal write, 1h costs 2x):

- instructions and tool definitions are identical across *runs*, so `1h` lets the
  next run read what this one wrote. Over seven runs that is ~2.6 prefix-units
  instead of ~8.75.
- the moving message breakpoint stays at `5m`: message history is per-run, so a
  1h write would be paid at 2x and never read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import ModelSettings
from pydantic_ai.capabilities import AbstractCapability

#: The provider prefix in ``model.model`` that these settings are valid for. The
#: keys are namespaced (``anthropic_*``) and mean nothing to the Ollama/OpenAI
#: model classes; sending them anyway would be a silent bet on how a foreign
#: backend treats unknown settings.
_PROVIDER = "anthropic/"

#: Stable across runs — the whole point of the longer retention.
_PREFIX_TTL = "1h"

#: Per-run, so it must not be written at the 2x one-hour premium.
_MESSAGES_TTL = "5m"


@dataclass
class PromptCacheCapability(AbstractCapability[Any]):
    """Enable Anthropic prompt caching when the model under test is an Anthropic one.

    ``main_model`` is the configured ``model.model`` string, passed in from
    ``agent_build.geo_capabilities()`` the same way ``MapOutputCapability`` gets it —
    a capability under ``chester/`` must not reach back into the agent factory.
    """

    main_model: str = ""

    def get_instructions(self):
        """No instructions. See the module docstring: this capability costs zero tokens."""
        return None

    def get_model_settings(self):
        if not self.main_model.strip().lower().startswith(_PROVIDER):
            return None
        return ModelSettings(
            anthropic_cache_instructions=_PREFIX_TTL,
            anthropic_cache_tool_definitions=_PREFIX_TTL,
            anthropic_cache=_MESSAGES_TTL,
        )
