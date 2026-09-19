"""Chester as a team — the orchestrator variant (scaffold, no content yet).

A sibling of chester-agent, not a layer above it: it uses chester-geo-tools
directly, never chester-agent and never chester-mcp. Both variants are adapters of
the same kind over one tool layer; whatever they share comes from the harness, not
from importing each other. `tests/test_packages.py` holds that direction.
"""
