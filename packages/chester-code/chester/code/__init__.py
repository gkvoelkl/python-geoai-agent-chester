"""Chester as a coder — every task done by one program it writes.

A third architecture beside chester-agent (one agent, one tool call per step) and
chester-team (an orchestrator over ressort agents): the agent has **no geo tools of its
own**. Every geo tool of chester-geo-tools is a function inside one tool, `run_code`
(pydantic-ai-harness CodeMode), and the agent solves a task by writing one Python
program that calls them — fetch, clip, compute, map — and returns paths and numbers.

It runs on chester-runtime (the gate hook, the run log, the shared CodeMode helpers)
like the other two, and is their sibling, not a layer: no import between chester-code
and chester-agent or chester-team. `tests/test_packages.py` holds the direction.
Concept and measurements: `doc/chester-code.md`.
"""
