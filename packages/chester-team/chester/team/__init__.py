"""Chester as a multi-agent — orchestrator and ressort agents (scaffold, no content yet).

Planned shape (orchestrator-worker over a pipeline): one orchestrator on SelmaKit
talks to the user, splits the task and calls ressort agents as tools; the ressorts
are cut along the phases of the chain — scout, acquisition, vector, raster/terrain,
output — not along domains. Checking is no ressort; the check tools stay visible to
all. Agents hand over paths, not data: workspace, GeoCache and provenance are the
blackboard. A ressort is no SelmaKit agent; it serves a slice of chester-geo-tools.

A sibling of chester-agent, not a layer above it: no import and no call between the
two (not via a subprocess, not over MCP). `tests/test_packages.py` holds the import
direction; the call direction is review.
"""
