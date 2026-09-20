"""Where a Chester agent reads its configuration — one reader for agent and team.

Moved out of the root `agent_build.py` (2026-09-19, KP.5 T0): chester-team's
orchestrator needs the same model, vision-model and geodata settings, and must not
import chester-agent to get them. Best-effort readers throughout: an unreadable
config must never be the reason a run cannot start.

Every reader takes ``config_name``/``state_dir``, because a bench run under a side
config (``testprompt.py --model``) must be built from **that** file. Reading the main
one instead is not a cosmetic slip: `PromptCacheCapability` and `ModelLimitsCapability`
switch on whether ``model.model`` names an Anthropic model, so a hosted run under a
side config would get no prompt cache and no ``max_tokens`` — the exact failure
`ModelLimitsCapability` exists to prevent (found 2026-09-20, walkthrough station 4).
"""

from __future__ import annotations

import json
from pathlib import Path

from chester import geoconfig
from chester.runtime.modellimits import DEFAULT_MAX_TOKENS

# Defined in chester.geoconfig so the LLM-free CLIs can read the same config
# without importing SelmaKit; re-exported here, where callers expect them.
STATE_DIR = geoconfig.STATE_DIR
CONFIG_NAME = geoconfig.CONFIG_NAME
WORKSPACE_DIR = f"{STATE_DIR}/workspace"


def load_geodata(config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR) -> dict:
    """The ``geodata`` block from ``.chester/chester.json`` (best-effort).

    Thin wrapper over :func:`chester.geoconfig.load_geodata`, which the LLM-free
    CLIs share so retention settings can't drift between agent and ``data.py``.
    """
    return geoconfig.load_geodata(state_dir, config_name)


def config_model_field(field: str, config_name: str = CONFIG_NAME,
                       state_dir: str = STATE_DIR) -> str:
    """One ``model.*`` string from the config, best-effort (missing → empty)."""
    try:
        cfg = json.loads((Path(state_dir) / config_name).read_text())
        return (cfg.get("model") or {}).get(field) or ""
    except (OSError, ValueError):
        return ""


def config_max_tokens(config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR) -> int:
    """``model.max_tokens`` from the config, or the capability's default.

    Its own reader rather than :func:`config_model_field`, which coerces to ``str``
    and would turn a deliberate ``0`` into ``""``. A bad value falls back instead of
    raising: an unreadable config must never be the reason a run cannot start.
    """
    try:
        cfg = json.loads((Path(state_dir) / config_name).read_text())
        return int((cfg.get("model") or {}).get("max_tokens") or DEFAULT_MAX_TOKENS)
    except (OSError, ValueError, TypeError):
        return DEFAULT_MAX_TOKENS


def config_base_url(config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR) -> str:
    """The ``model.base_url`` from the config (the Ollama OpenAI endpoint)."""
    return config_model_field("base_url", config_name, state_dir)


def config_vision_model(config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR) -> str:
    """The ``model.vision_model`` fallback from the config (may be empty).

    Used whenever the main model cannot look at a snapshot itself — either because
    it says so, or because ``chester.visioncaps`` established beforehand that it
    takes no image input at all. Empty → no fallback available, and the visual
    check goes inert instead of aborting the run (MapOutput's ``inspect_map``).
    """
    return config_model_field("vision_model", config_name, state_dir)


def config_main_model(config_name: str = CONFIG_NAME, state_dir: str = STATE_DIR) -> str:
    """The ``model.model`` under test — the one whose vision support decides routing."""
    return config_model_field("model", config_name, state_dir)


def config_block(name: str, config_name: str = CONFIG_NAME,
                 state_dir: str = STATE_DIR) -> dict:
    """One top-level block of the config (e.g. ``team``), best-effort — missing → ``{}``.

    chester-team keeps its settings in a block of its own (decided 2026-09-19), so
    agent and team can run side by side and a config without the block still loads.
    ``config_name`` follows the run: a bench run under a side config
    (``testprompt.py --model``) must read *that* file, or the ressorts run on another
    model than the record says (found in review, 2026-09-20).
    """
    try:
        cfg = json.loads((Path(state_dir) / config_name).read_text())
    except (OSError, ValueError):
        return {}
    block = cfg.get(name)
    return block if isinstance(block, dict) else {}
