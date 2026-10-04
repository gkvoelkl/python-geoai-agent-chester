"""The MCP server says what it is for — and nothing about how to work.

Measured 2026-10-04 (F+MCP, `tallest-buildings-map`): without the protocol's prompt
suffix, Claude Desktop answered from a web search and never called chester. It lists
deferred MCP servers by name only, and the server introduced itself with nothing. The
pointer fixes the first half. The second half is the measurement: cell F+MCP compares
Chester's tools *without* Chester's guidance, so a method rule in this text would
quietly turn F+MCP into F+.
"""

from __future__ import annotations

import pytest

from chester import mcpserver

#: Words that would make the pointer a rule block: clipping, CRS, checking, recipes.
METHOD_WORDS = ("clip", "crs", "epsg", "metric", "validate", "buffer", "always", "never",
                "must", "first")


def test_the_server_carries_the_pointer(tmp_path):
    pytest.importorskip("fastmcp")
    server = mcpserver.build_server(str(tmp_path), tools=[])
    assert server.instructions == mcpserver.SERVER_INSTRUCTIONS


def test_the_pointer_names_purpose_and_reach():
    text = mcpserver.SERVER_INSTRUCTIONS
    assert "Germany, Switzerland and Austria" in text
    assert "official data" in text and "instead of quoting figures from the web" in text
    assert "'chester'" in text, "a deferred client must know what to search for"


def test_the_pointer_carries_no_method():
    lowered = mcpserver.SERVER_INSTRUCTIONS.lower()
    found = [w for w in METHOD_WORDS if w in lowered.split() or f" {w}" in lowered]
    assert not found, f"method in the pointer — F+MCP would measure guidance: {found}"
