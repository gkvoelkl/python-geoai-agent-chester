"""Where a Chester agent reads its configuration — one reader for agent and team.

Moved out of the root `agent_build.py` (2026-09-19, KP.5 T0): chester-team's
orchestrator needs the same model, vision-model and geodata settings, and must not
import chester-agent to get them. Best-effort readers throughout: an unreadable
config must never be the reason a run cannot start.
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


def load_geodata() -> dict:
    """The ``geodata`` block from ``.chester/chester.json`` (best-effort).

    Thin wrapper over :func:`chester.geoconfig.load_geodata`, which the LLM-free
    CLIs share so retention settings can't drift between agent and ``data.py``.
    """
    return geoconfig.load_geodata(STATE_DIR, CONFIG_NAME)


def config_model_field(field: str) -> str:
    """One ``model.*`` string from the config, best-effort (missing → empty)."""
    try:
        cfg = json.loads((Path(STATE_DIR) / CONFIG_NAME).read_text())
        return (cfg.get("model") or {}).get(field) or ""
    except (OSError, ValueError):
        return ""


def config_max_tokens() -> int:
    """``model.max_tokens`` from the config, or the capability's default.

    Its own reader rather than :func:`config_model_field`, which coerces to ``str``
    and would turn a deliberate ``0`` into ``""``. A bad value falls back instead of
    raising: an unreadable config must never be the reason a run cannot start.
    """
    try:
        cfg = json.loads((Path(STATE_DIR) / CONFIG_NAME).read_text())
        return int((cfg.get("model") or {}).get("max_tokens") or DEFAULT_MAX_TOKENS)
    except (OSError, ValueError, TypeError):
        return DEFAULT_MAX_TOKENS


def config_base_url() -> str:
    """The ``model.base_url`` from the config (the Ollama OpenAI endpoint)."""
    return config_model_field("base_url")


def config_vision_model() -> str:
    """The ``model.vision_model`` fallback from the config (may be empty).

    Used whenever the main model cannot look at a snapshot itself — either because
    it says so, or because ``chester.visioncaps`` established beforehand that it
    takes no image input at all. Empty → no fallback available, and the visual
    check goes inert instead of aborting the run (MapOutput's ``inspect_map``).
    """
    return config_model_field("vision_model")


def config_main_model() -> str:
    """The ``model.model`` under test — the one whose vision support decides routing."""
    return config_model_field("model")
