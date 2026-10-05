"""ModelLimitsCapability: the output budget goes to Anthropic — and to nobody else.

The provider gate is the load-bearing part, for the same reason as in
`test_promptcache.py`: the measurement series compares cells on the same machine, and a
model setting that seeps into the Ollama path would change cell L+.

The occasion was a measured failure (2026-09-13, F+ on
`heldout-regensburg-danube-bridges`): without `max_tokens` a hosted run inherits the
provider default and dies after 18 tool calls **before** a single character of answer
exists — verdict 0/5 at a tool coverage of 0.75.
"""

from __future__ import annotations

import pytest

from chester.runtime.modellimits import DEFAULT_MAX_TOKENS, ModelLimitsCapability


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4-8",
        "  Anthropic/claude-sonnet-5  ",  # whitespace and capitals are no change of provider
    ],
)
def test_anthropic_models_get_a_budget(model):
    settings = ModelLimitsCapability(main_model=model).get_model_settings()
    assert settings is not None, f"{model!r} bekommt kein Ausgabebudget"
    assert settings["max_tokens"] == DEFAULT_MAX_TOKENS


@pytest.mark.parametrize(
    "model",
    [
        "ollama/gemma4:26b-mlx",
        "openai/gpt-4o",
        "gemma4:26b-mlx",  # without a prefix selmakit reads this as ollama
        "",
    ],
)
def test_other_providers_are_left_untouched(model):
    assert ModelLimitsCapability(main_model=model).get_model_settings() is None, (
        f"{model!r} bekommt ein Anthropic-Budget — das ändert die Zelle L+"
    )


def test_the_configured_value_wins():
    settings = ModelLimitsCapability(
        main_model="anthropic/claude-sonnet-5", max_tokens=64000
    ).get_model_settings()
    assert settings is not None
    assert settings["max_tokens"] == 64000


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_budget_sets_nothing(value):
    """`0` means "do not set", not "zero tokens" — otherwise nothing would answer at all."""
    assert ModelLimitsCapability(
        main_model="anthropic/claude-sonnet-5", max_tokens=value
    ).get_model_settings() is None


def test_it_costs_nothing_in_the_prompt():
    """A token budget is a runtime setting, not a rule for the model."""
    assert ModelLimitsCapability(main_model="anthropic/claude-sonnet-5").get_instructions() is None


def test_the_key_is_the_one_pydantic_ai_defines():
    """Guards against a silent rename: an unknown key is ignored."""
    from pydantic_ai.settings import ModelSettings

    settings = ModelLimitsCapability(main_model="anthropic/claude-sonnet-5").get_model_settings()
    assert settings is not None
    unknown = set(settings) - set(ModelSettings.__annotations__)
    assert not unknown, f"Einstellungen, die pydantic-ai nicht kennt: {sorted(unknown)}"


def test_both_hosted_capabilities_merge_without_collision():
    """Cache settings and budget come from two capabilities — shared keys would have
    meant that one overwrites the other."""
    from chester.runtime.promptcache import PromptCacheCapability

    cache = PromptCacheCapability(main_model="anthropic/claude-sonnet-5").get_model_settings()
    limits = ModelLimitsCapability(main_model="anthropic/claude-sonnet-5").get_model_settings()
    assert cache is not None and limits is not None
    assert not (set(cache) & set(limits)), (
        "gemeinsame Schlüssel — eine Fähigkeit überschriebe die andere"
    )
