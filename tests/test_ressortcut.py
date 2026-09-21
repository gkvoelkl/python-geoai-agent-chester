"""Test-Level 1: the ressort cut covers the tool surface — exactly, and silently never less.

A cut that loses a tool does so without a sound: the ressort agent simply never sees
it, and a probe that needs it fails for a reason nobody looks for. The same class as
`op_tools` and the blind tool-coverage sensor — a collector that finds less than is
there. So the cut is held to the live wrapper layer here, in both directions.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from chester import ressortcut, toolchoice

ROOT = Path(__file__).resolve().parent.parent


def _wrapper_tools() -> set[str]:
    from chester import mcpserver

    return {t.__name__ for t in mcpserver.collect_tools("/tmp/chester-ressortcut")}


def _placed() -> list[str]:
    return ([t for tools in ressortcut.RESSORTS.values() for t in tools]
            + list(ressortcut.CHECKS) + list(ressortcut.MCP_ONLY))


def test_every_wrapper_tool_has_exactly_one_place():
    counts = Counter(_placed())
    twice = sorted(t for t, n in counts.items() if n > 1)
    missing = sorted(_wrapper_tools() - set(counts))
    assert not twice, f"in more than one place: {twice}"
    assert not missing, f"in no ressort, not a check, not MCP-only: {missing}"


def test_the_cut_names_no_tool_that_does_not_exist():
    unknown = sorted(set(_placed()) - _wrapper_tools())
    assert not unknown, f"named in the cut, not in the wrapper layer: {unknown}"


def test_every_ressort_gets_the_checks():
    for name in ressortcut.RESSORTS:
        tools = ressortcut.tools_for(name)
        assert set(ressortcut.CHECKS) <= set(tools), name
    assert ressortcut.ressort_of("check_crs") == "checks"
    assert ressortcut.ressort_of("vector_buffer") == "vector"
    assert ressortcut.ressort_of("geo_python_run") is None  # agent-level, placed in T2


def test_a_borrowed_tool_keeps_its_owner():
    """A loan, not a second home: the output ressort may reproject so a map does not
    fail on a CRS, but `vector_reproject` still belongs to the vector ressort."""
    assert "vector_reproject" in ressortcut.tools_for("output")
    assert "vector_reproject" not in ressortcut.RESSORTS["output"]
    assert ressortcut.ressort_of("vector_reproject") == "vector"
    for ressort, lent in ressortcut.LENT.items():
        assert ressort in ressortcut.RESSORTS
        for tool in lent:
            owner = ressortcut.ressort_of(tool)
            assert owner and owner != ressort, f"{tool} is not borrowed from anywhere"


def test_no_ressort_gets_an_mcp_only_tool():
    """`validate_result` is the gate without enforcement and `read_artifact` reads files
    an in-process agent opens anyway — both exist for a foreign client (2026-09-20)."""
    served = {t for name in ressortcut.RESSORTS for t in ressortcut.tools_for(name)}
    assert not served & set(ressortcut.MCP_ONLY)
    assert ressortcut.ressort_of("validate_result") == "mcp-only"


def test_the_probe_bank_uses_the_same_ressort_names():
    """toolchoice takes its names from the cut — one source, not two lists."""
    assert toolchoice.RESSORTS == tuple(ressortcut.RESSORTS)


def test_each_probe_expects_its_tools_in_its_ressort():
    """A probe whose expected tool sits in another ressort would measure the orchestrator
    against a routing the cut itself contradicts."""
    rows = [json.loads(line) for line in (ROOT / "agent-probe-tasks.jsonl")
            .read_text(encoding="utf-8").splitlines() if line.strip()]
    wrong = {}
    for task in rows:
        placed = {ressortcut.ressort_of(t) for t in task.get("expected_tools", [])} - {None}
        if placed and task.get("expected_ressort") not in placed:
            wrong[task["id"]] = (task.get("expected_ressort"), sorted(placed))
    assert not wrong, f"probe ressort disagrees with the cut: {wrong}"


def test_every_module_in_the_cut_carries_its_instructions():
    """A ressort's instructions are assembled from the modules its tools come from. A
    module without text would hand its tools to a ressort agent with no guidance —
    silently, the same way a lost tool would. MCP-only modules are exempt: MCP has no
    instruction channel, the docstring is the whole text there."""
    import importlib

    from chester import mcpserver

    mcp_only = {"gatetools", "artifacttools"}
    bare = []
    for name in mcpserver.wrapper_modules():
        if name in mcp_only:
            continue
        mod = importlib.import_module(f"chester.{name}")
        text = mod.instructions() if callable(getattr(mod, "instructions", None)) \
            else getattr(mod, "INSTRUCTIONS", "")
        if not str(text).strip():
            bare.append(name)
    assert not bare, f"wrapper modules without instructions: {bare}"


def test_finding_and_fetching_are_one_ressort():
    """They were two — a scout that looks and an acquisition that fetches — and the
    boundary cost more than it bought (2026-09-21): both shared 9 wrapper modules, and
    the orchestrator planned "find, then fetch" as two steps for one piece of work."""
    data = set(ressortcut.RESSORTS["data"])
    assert {"wfs_capabilities", "geodata_search", "geocode", "geocache_list"} <= data
    assert {"osm_features", "wfs_features", "fetch_boundaries", "stats_table"} <= data
    assert "scout" not in ressortcut.RESSORTS and "acquisition" not in ressortcut.RESSORTS
    for tool in data:
        assert ressortcut.ressort_of(tool) == "data", tool
