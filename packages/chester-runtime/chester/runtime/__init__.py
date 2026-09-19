"""Chester's runtime on SelmaKit — what every Chester agent needs, whatever its shape.

Shared by chester-agent (one agent) and chester-team (orchestrator + ressort agents):
the enforcing gate hook, the observer and guard capabilities (run log, plan guard,
prompt cache, model limits, skill guide) and the vision-model call. It depends on
chester-geo-tools and SelmaKit/pydantic-ai, never on agent, team or mcp — so the two
variants share one gate instead of two copies drifting apart.
"""
