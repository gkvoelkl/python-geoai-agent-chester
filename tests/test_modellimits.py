"""ModelLimitsCapability: das Ausgabebudget geht an Anthropic — und an sonst niemanden.

Die Anbieterschranke ist der tragende Teil, aus demselben Grund wie bei
`test_promptcache.py`: Die KO-Reihe vergleicht Zellen derselben Maschine, und eine
Modelleinstellung, die in den Ollama-Pfad sickert, veränderte die Zelle L+.

Anlass war ein gemessener Ausfall (2026-09-13, F+ auf
`heldout-regensburg-danube-bridges`): Ohne `max_tokens` erbt ein gehosteter Lauf die
Provider-Vorgabe und stirbt nach 18 Werkzeugaufrufen, **bevor** ein Zeichen Antwort
entsteht — Urteil 0/5 bei einer Werkzeugabdeckung von 0,75.
"""

from __future__ import annotations

import pytest

from chester.capabilities import DEFAULT_MAX_TOKENS, ModelLimitsCapability


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-4-8",
        "  Anthropic/claude-sonnet-5  ",  # Leerraum und Großschreibung sind kein Anbieterwechsel
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
        "gemma4:26b-mlx",  # ohne Präfix liest selmakit das als ollama
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
    """`0` heisst „nicht setzen", nicht „null Token" — sonst antwortete gar nichts mehr."""
    assert ModelLimitsCapability(
        main_model="anthropic/claude-sonnet-5", max_tokens=value
    ).get_model_settings() is None


def test_it_costs_nothing_in_the_prompt():
    """Ein Tokenbudget ist eine Laufzeiteinstellung, keine Regel für das Modell."""
    assert ModelLimitsCapability(main_model="anthropic/claude-sonnet-5").get_instructions() is None


def test_the_key_is_the_one_pydantic_ai_defines():
    """Schützt vor einer stillen Umbenennung: Ein unbekannter Schlüssel wird ignoriert."""
    from pydantic_ai.settings import ModelSettings

    settings = ModelLimitsCapability(main_model="anthropic/claude-sonnet-5").get_model_settings()
    assert settings is not None
    unknown = set(settings) - set(ModelSettings.__annotations__)
    assert not unknown, f"Einstellungen, die pydantic-ai nicht kennt: {sorted(unknown)}"


def test_both_hosted_capabilities_merge_without_collision():
    """Cache-Einstellungen und Budget kommen aus zwei Fähigkeiten —
    gemeinsame Schlüssel hätten bedeutet, dass eine die andere überschreibt."""
    from chester.capabilities import PromptCacheCapability

    cache = PromptCacheCapability(main_model="anthropic/claude-sonnet-5").get_model_settings()
    limits = ModelLimitsCapability(main_model="anthropic/claude-sonnet-5").get_model_settings()
    assert cache is not None and limits is not None
    assert not (set(cache) & set(limits)), (
        "gemeinsame Schlüssel — eine Fähigkeit überschriebe die andere"
    )
