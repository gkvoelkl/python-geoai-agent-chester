"""Test-Level 1: the orchestrator of chester-team and the team's own configuration.

No model is asked. What is under test is the surface the orchestrator is given (one
tool per ressort, the checks, no geo tool directly), the derived configuration that
lets agent and team run side by side, and that the team is wired like the agent —
gate and commands included.
"""

from __future__ import annotations

import json

from chester import ressortcut
from chester.team import orchestrator


def test_the_orchestrator_touches_no_geodata_itself():
    names = [t.__name__ for t in orchestrator.orchestrator_tools("/tmp/chester-orch")]
    ressort_tools = [f"ressort_{n}" for n in ressortcut.RESSORTS]
    assert names[: len(ressort_tools)] == ressort_tools
    assert set(names) == set(ressort_tools) | set(ressortcut.CHECKS) | {"inspect_map"}
    geo = {t for tools in ressortcut.RESSORTS.values() for t in tools}
    assert not geo & set(names), "a geo tool leaked onto the orchestrator"


def test_each_ressort_tool_says_what_it_hands_back():
    ressort_tools = orchestrator.orchestrator_tools("/tmp/chester-orch")[:len(ressortcut.RESSORTS)]
    for tool in ressort_tools:
        assert "outputs" in tool.__doc__ and "capped" in tool.__doc__


def test_the_team_block_defaults_stay_clear_of_the_agent():
    block = orchestrator.team_block({})
    assert block["webchat_port"] != 8000 and block["dashboard_port"] != 8501
    assert len({block["webchat_port"], block["dashboard_port"], block["bench_port"]}) == 3


def test_the_effective_config_applies_the_team_block(tmp_path):
    main = {"model": {"model": "ollama/x"},
            "channels": {"webchat": {"port": 8000}, "telegram": {"enabled": True}},
            "team": {"webchat_port": 8123}}
    (tmp_path / "chester.json").write_text(json.dumps(main))
    name = orchestrator.effective_config("chester.json", str(tmp_path))
    derived = json.loads((tmp_path / name).read_text())
    assert name == "chester.team.json"
    assert derived["channels"]["webchat"]["port"] == 8123
    assert derived["channels"]["telegram"]["enabled"] is False, "two bots on one token"
    assert derived["model"] == main["model"], "everything else is the main config"
    assert json.loads((tmp_path / "chester.json").read_text()) == main, "source untouched"


def test_a_config_without_a_team_block_still_builds_the_team(tmp_path):
    (tmp_path / "chester.json").write_text(json.dumps({"model": {"model": "ollama/x"}}))
    name = orchestrator.effective_config("chester.json", str(tmp_path))
    derived = json.loads((tmp_path / name).read_text())
    assert derived["channels"]["webchat"]["port"] == orchestrator.DEFAULT_WEBCHAT_PORT


def test_the_team_is_wired_like_the_agent(tmp_path, monkeypatch):
    """Gate and commands, as in `gateway.py` — an agent without the gate is one harness
    level below the product (the drift `test_structure` guards for the agent)."""
    from chester.runtime import commands, wiring

    seen: list[str] = []
    routes_seen: list = []
    where: list = []

    def gate(agent, workspace_dir=None, state_dir=None, routes=None):
        seen.append("gate")
        routes_seen.append(routes)
        where.append((workspace_dir, state_dir))

    monkeypatch.setattr(wiring, "register_validation_gate", gate)
    monkeypatch.setattr(commands, "register_runtime_commands",
                        lambda agent, workspace_dir=None: where.append(workspace_dir)
                        or seen.append("commands"))
    (tmp_path / "chester.json").write_text(json.dumps({"model": {"model": "ollama/x"}}))
    orchestrator.build_team_gateway("chester.json", str(tmp_path))
    assert sorted(seen) == ["commands", "gate"]
    from chester.runtime.gatehook import TEAM_ROUTES

    assert routes_seen == [TEAM_ROUTES], "the team's gate must name ressorts, not agent tools"
    # Gate and commands must work where this team works, not in the default state dir.
    assert where == [(f"{tmp_path}/workspace", str(tmp_path)), f"{tmp_path}/workspace"]
    kinds = [type(c).__name__ for c in orchestrator.team_capabilities(str(tmp_path))]
    assert kinds[-1] == "OrchestratorCapability" and "RunLogCapability" in kinds
    assert not any(k.startswith(("Vector", "DataDiscovery", "GeoCore")) for k in kinds)


def test_the_orchestrator_gets_no_skills_and_no_recipes(monkeypatch):
    """It read the `walkability` skill and passed `qgis_service_area` down to a ressort
    as an instruction — for a tool that does not exist with QGIS off (2026-09-21). A
    skill is a recipe for an agent that has tools; the orchestrator hands out goals."""
    from chester.runtime import wiring

    kinds = [type(c).__name__ for c in orchestrator.team_capabilities("/tmp/chester-orch")]
    assert "GeoSkillGuideCapability" not in kinds, "no catalogue it cannot use"
    flat = " ".join(orchestrator._INSTRUCTIONS.lower().split())  # the text is wrapped
    assert "do not name tools" in flat and "goals, not recipes" in flat

    Skills = type("Skills", (), {})
    Cron = type("CronCapability", (), {})
    Web = type("WebSearch", (), {})
    monkeypatch.setattr(wiring, "default_capabilities", lambda ctx: [Skills(), Cron(), Web()])
    kept = [type(c).__name__ for c in wiring.capability_filter(frozenset({"Skills"}))(None)]
    assert kept == ["WebSearch"], "Chester's drops plus the variant's"
    assert [type(c).__name__ for c in wiring.selmakit_capabilities(None)] == [
        "Skills", "WebSearch"], "the single agent keeps its skills"
