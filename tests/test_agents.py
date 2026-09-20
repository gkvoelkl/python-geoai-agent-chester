"""The one place an agent is built — and that it is built from the run's own config.

`agents.build_agent` replaced six copies of the same block (structure test:
`test_nobody_else_builds_an_agent`). What it must get right beyond the capability set
and the gate: a bench run under a **side config** (`testprompt.py --model X`) has to
be built from that file. Reading the main config instead is not cosmetic — the prompt
cache and the token limit switch on whether `model.model` names an Anthropic model,
so a hosted run would get neither, which is the failure `ModelLimitsCapability` exists
to prevent (found in the walkthrough, 2026-09-20).
"""

from __future__ import annotations

import json

from chester.runtime.wiring import base_capabilities


def _side_config(tmp_path, model: str) -> str:
    (tmp_path / "side.json").write_text(json.dumps({"model": {"model": model,
                                                              "max_tokens": 4242}}))
    return "side.json"


def _by_name(capabilities: list) -> dict:
    return {type(c).__name__: c for c in capabilities}


def test_the_base_set_reads_the_model_of_the_run_s_own_config(tmp_path):
    name = _side_config(tmp_path, "anthropic/claude-sonnet-5")
    caps = _by_name(base_capabilities(str(tmp_path / "workspace"),
                                      config_name=name, state_dir=str(tmp_path)))
    assert caps["PromptCacheCapability"].main_model == "anthropic/claude-sonnet-5"
    assert caps["ModelLimitsCapability"].main_model == "anthropic/claude-sonnet-5"
    assert caps["ModelLimitsCapability"].max_tokens == 4242


def test_a_local_model_leaves_both_provider_capabilities_inert(tmp_path):
    """They are no-ops for a local model — that is the whole point of the gate on the
    provider; the test pins that the *name* they judge comes from the side config."""
    name = _side_config(tmp_path, "ollama/gemma4:26b-mlx")
    caps = _by_name(base_capabilities(str(tmp_path / "workspace"),
                                      config_name=name, state_dir=str(tmp_path)))
    assert caps["PromptCacheCapability"].main_model.startswith("ollama/")
    assert caps["ModelLimitsCapability"].main_model.startswith("ollama/")
