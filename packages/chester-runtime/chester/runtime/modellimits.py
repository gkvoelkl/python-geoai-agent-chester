"""ModelLimitsCapability — give the hosted model an output budget it can finish in.

``max_tokens`` caps **thinking plus answer together**. SelmaKit's ``ModelConfig``
has no field for it (only ``model``, ``base_url``, ``api_key``, ``timeout_seconds``,
``thinking``), so pydantic-ai falls back to the *provider default* — and for
Anthropic that default is small. The Ollama path never noticed.

Measured 2026-09-13, cell F+ on ``heldout-regensburg-danube-bridges``
(`claude-sonnet-5`, thinking high): after 18 tool calls the run died with
``UnexpectedModelBehavior: Model token limit (provider default) exceeded before any
response was generated``. Coverage was 0.75 — the tool chain was on the right track
— and the verdict was 0/5 because nothing was ever written. The cell was not
measuring the model, it was measuring a default.

**Provider-scoped, like PromptCacheCapability and for the same reason.** The KO
series only means something if the local cell L+ stays bit-identical to the build it
stands in for, so a setting that leaked into the Ollama path would change the thing
being measured. Settings from several capabilities are merged into a chain by
pydantic-ai, so this sits next to the cache settings without colliding.

**Why not simply the maximum.** Sonnet 5 allows 128k output tokens, but a large
budget on a non-streaming request invites HTTP timeouts, and a run that thinks until
the budget is gone is worse than one that answers. 32k is roughly ten times the
longest answer the bank has produced and still finishes inside
``model.timeout_seconds``. Set ``model.max_tokens`` in ``.chester/chester.json`` to
override; the value travels into the record of every judged run through the config,
not through code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import ModelSettings
from pydantic_ai.capabilities import AbstractCapability

#: The provider prefix this budget is for. Ollama gets its ceiling from the server.
_PROVIDER = "anthropic/"

#: Used when ``model.max_tokens`` is absent from the config. See the module docstring.
DEFAULT_MAX_TOKENS = 32000


@dataclass
class ModelLimitsCapability(AbstractCapability[Any]):
    """Set ``max_tokens`` when the model under test is an Anthropic one.

    ``main_model`` is the configured ``model.model`` string, passed in from
    ``agent_build.geo_capabilities()`` — a capability under ``chester/`` must not
    reach back into the agent factory.
    """

    main_model: str = ""
    max_tokens: int = DEFAULT_MAX_TOKENS

    def get_instructions(self):
        """No instructions. A token budget is a runtime setting, not a rule for the model."""
        return None

    def get_model_settings(self):
        if not self.main_model.strip().lower().startswith(_PROVIDER):
            return None
        if self.max_tokens <= 0:
            return None
        return ModelSettings(max_tokens=self.max_tokens)
