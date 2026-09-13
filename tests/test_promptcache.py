"""PromptCacheCapability: the settings go out for Anthropic — and for nobody else.

The provider gate is the load-bearing part. Chester's KO series compares an ablated
build against the current one, and that only means something if the local cell stays
bit-identical to the build it stands in for. A model setting that leaked into the
Ollama path would change the thing being measured.
"""

from __future__ import annotations

import pytest

from chester.capabilities import PromptCacheCapability


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-opus-4-8",
        "anthropic/claude-sonnet-5",
        "  Anthropic/claude-opus-4-8  ",  # whitespace and case are not a provider change
    ],
)
def test_anthropic_models_get_the_cache_settings(model):
    settings = PromptCacheCapability(main_model=model).get_model_settings()
    assert settings is not None, f"{model!r} bekommt keine Cache-Einstellungen"
    # Instructions and tool definitions are identical across runs, so they are written
    # once per hour rather than once per run; the message breakpoint is per-run.
    assert settings["anthropic_cache_instructions"] == "1h"
    assert settings["anthropic_cache_tool_definitions"] == "1h"
    assert settings["anthropic_cache"] == "5m"


@pytest.mark.parametrize(
    "model",
    [
        "ollama/gemma4:26b-mlx",
        "openai/gpt-4o",
        "google/gemini-2.0-flash",
        "gemma4:26b-mlx",  # bare string — selmakit reads this as ollama
        "",
    ],
)
def test_other_providers_are_left_untouched(model):
    assert PromptCacheCapability(main_model=model).get_model_settings() is None, (
        f"{model!r} bekommt Anthropic-Einstellungen — das ändert die Zelle L+"
    )


def test_it_costs_nothing_in_the_prompt():
    """No instructions, or the capability would inflate what it exists to make cheap."""
    assert PromptCacheCapability(main_model="anthropic/claude-opus-4-8").get_instructions() is None


def test_the_settings_keys_are_the_ones_pydantic_ai_defines():
    """Guards against a silent rename: an unknown key is ignored, not rejected."""
    from pydantic_ai.models.anthropic import AnthropicModelSettings

    settings = PromptCacheCapability(main_model="anthropic/claude-opus-4-8").get_model_settings()
    assert settings is not None
    unknown = set(settings) - set(AnthropicModelSettings.__annotations__)
    assert not unknown, f"Einstellungen, die pydantic-ai nicht kennt: {sorted(unknown)}"
