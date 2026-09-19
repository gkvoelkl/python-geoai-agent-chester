"""The enforcing validation gate — the agent half of ``chester.gate``.

Chester's validation was instruction-driven: the tools (`check_crs`,
`sanity_check_result`) existed, but nothing stopped the model from reporting an
unchecked result. This module turns the checks in ``chester.gate`` into an enforced
loop phase — a post-run validator registered via SelmaKit's
``Agent.output_validator`` passthrough.

What it does, per run:

1. Read the session's strictness ``level`` (``/valid_level``, default 1). Level 0
   → the answer passes through untouched.
2. Find datasets the **current run produced** *and* the **answer mentions** — both
   conditions, so pure Q&A/number answers and invisible intermediate files stay
   untouched (§4, step 1). "Produced this run" comes from the run-scoped
   ``tool_returns(ctx)`` (SelmaKit ``dec0e62``, ``run_id``-based — no time-window
   heuristic); "mentioned" is a basename/stem match against the final text.
3. Run the **level-1 structural checks in-process** (empty result, invalid/null/
   empty geometry, missing CRS) — cheap, deterministic, no model roundtrip.
4. Clean → return the answer unchanged (no retry, no cost in the normal case). A
   real defect → raise ``ModelRetry`` **once** (file + concrete defect); if the
   retry budget is already spent, let the answer through with an appended warning
   (loop-trap protection for weak models).

Levels 2 (visual) and 3 (redundancy) add advisory second opinions on a structurally
clean result — a note, never a retry.

Lives in chester-agent because it needs pydantic-ai, SelmaKit's session meta and the
map renderer of ``chester.capabilities.mapoutput``. The checks themselves are in
``chester.gate`` (chester-geo-tools), so Chester-MCP shares them without this layer.
"""

from __future__ import annotations

import asyncio
from typing import Any

from chester.gate import (
    DEFAULT_LEVEL,
    VALID_LEVEL_KEY,
    _absent_claims,
    _area_identity_problems,
    _bbox_extent_problem,
    _candidate_paths,
    _dead_link_targets,
    _format_problems,
    _mentioned,
    _redundancy_problems,
    _structural_problems,
    _unquoted_view_paths,
    clamp_level,
)
from chester.workspace import DEFAULT_WORKSPACE


def _may_retry(ctx: Any) -> bool:
    """Is the run's single retry still available?

    Every hard tier asks the same question, and each copy of the four lines was a
    chance to get the budget wrong. Retry **exactly once** (the concept's „einmalig
    ModelRetry") and never past the framework's own output-retry budget: a second
    failure raises instead of retrying, and looping a weak model is worse than a
    warning.
    """
    return _retry_allowance(ctx, budget=1)


def _may_retry_answer_only(ctx: Any) -> bool:
    """Is a retry available for a defect that costs **no tool call** to fix?

    Tier 1d — the answer names a result without its path — is the mildest finding
    the gate makes and therefore the last in line, so any substantive defect takes
    the single retry first. Measured 2026-09-05
    (`supermarket-accessibility-choropleth`): the extent tier fired, the agent
    clipped and recomputed (18 supermarkets became the correct 80), and by the time
    the dead link surfaced the budget was gone. A run with two defects is exactly
    the run where the mild one is starved — every time, by construction.

    Reserving a **second** retry for it is safe in a way a second substantive retry
    would not be: the fix is to re-emit the same answer with a path pasted in, no
    tools, no geoprocessing, no chance of looping on a computation the model cannot
    do. That is the distinction `_may_retry`'s "looping a weak model is worse than a
    warning" is really about.

    **Live since SelmaKit 0.1.36** (2026-09-06), which ships
    ``_DEFAULT_RETRIES = {"tools": 4, "output": 2}`` — requested as
    `gkvoelkl/python-selmakit` issue #1. Before that this returned exactly what
    `_may_retry` returns, and the tier could only ever leave a note. With
    ``max_retries == 1`` it still degrades to that, so an older SelmaKit changes
    nothing but the reach of this tier.
    """
    return _retry_allowance(ctx, budget=2)


def _retry_allowance(ctx: Any, budget: int) -> bool:
    """Shared arithmetic: never past the framework's own output-retry budget."""
    retry = getattr(ctx, "retry", 0) or 0
    max_retries = getattr(ctx, "max_retries", 1)
    if max_retries is None:
        max_retries = 1
    return retry < min(budget, max_retries)



# The focused review question — a parseable verdict, not free prose. The vision
# model judges only GROSS errors (the class numbers miss) and answers OK / PROBLEM /
# NO_IMAGE, so a text-only model that can't see the image is inert, not a false hit.
_VISUAL_PROMPT = (
    "You are a GIS QA reviewer looking at a rendered snapshot of a result layer. "
    "Judge ONLY for gross errors: data placed in the wrong location (off-coast or "
    "wrong hemisphere ⇒ a CRS bug), an implausible extent/scale, or partition "
    "polygons that clearly overlap or leave big gaps. Ignore styling and minor "
    "detail. If it looks plausible, reply exactly 'OK'. If something is clearly "
    "wrong, reply 'PROBLEM: <one short reason>'. If you cannot see an image, reply "
    "'NO_IMAGE'."
)


def _visual_problems(path: str, *, vision_model: str, base_url: str, workspace: str) -> list[str]:
    """Level-2 visual check (V4): render the layer, ask the configured vision model.

    Returns **advisory** findings (0 or 1) — the visual verdict is a subjective
    second modality (``doc/visual-validation.md`` §7), so the gate appends it as a
    note rather than forcing a retry. Inert (``[]``) when no vision model is
    configured, when rendering or the vision call fails, or when the model reports
    it cannot see the image — a documented limitation, never a false defect.
    Synchronous (renders + a blocking ``run_sync`` vision turn); the gate calls it
    off the event loop via ``asyncio.to_thread``.
    """
    if not vision_model:
        return []
    try:
        from chester.mapsnapshot import _render_snapshot
        from chester.runtime.vision import _ask_vision_model
    except Exception:  # noqa: BLE001 - the visual channel is optional
        return []
    try:
        png, summary = _render_snapshot(
            [path], workspace, None, "quantiles", 5, "viridis", "Validation snapshot"
        )
        # The summary rides along: without it the reviewer guesses what the colours
        # mean and invents the rest (`doc/visual-validation.md` §7, 2026-08-26).
        verdict = _ask_vision_model(vision_model, base_url, png, _VISUAL_PROMPT, summary)
    except Exception:  # noqa: BLE001 - a render/vision failure must not break the run
        return []
    v = (verdict or "").strip()
    if v.upper().startswith("PROBLEM"):
        reason = v.split(":", 1)[1].strip() if ":" in v else v
        return [f"visual check flags a possible error: {reason}"]
    return []



def _read_level(sessions_dir: str, session_key: Any) -> int:
    """Read the session's validation level (default 1), via SelmaKit's SessionProxy.

    Reuses ``SessionProxy`` so the gate reads exactly the meta file ``/valid_level``
    writes (one source of truth). A synthetic run with a non-string ``deps`` (no
    real session) falls back to the default level.
    """
    if not isinstance(session_key, str):
        return DEFAULT_LEVEL
    try:
        from selmakit.commands import SessionProxy

        raw = SessionProxy(sessions_dir, session_key).get(VALID_LEVEL_KEY, DEFAULT_LEVEL)
    except Exception:  # noqa: BLE001 - a meta-read failure must not break the run
        return DEFAULT_LEVEL
    return clamp_level(raw)


def make_validation_gate(  # noqa: C901
    # C901 exception: gate levels 0-3; each level is a branch, that is the design
    *,
    sessions_dir: str,
    workspace: str = DEFAULT_WORKSPACE,
    vision_model: str = "",
    base_url: str = "",
):
    """Build the ``output_validator`` coroutine for Chester's runs.

    ``sessions_dir`` locates the per-session meta (for the strictness level);
    ``workspace`` is where produced paths resolve. ``vision_model``/``base_url`` (from
    ``model.vision_model`` in the config) enable the level-2 visual check; empty →
    that check is inert. Register the returned function via
    ``agent.output_validator(...)`` (see ``agent_build.register_validation_gate``).

    Two tiers, matching the concept's levels:
    - **Structural (level ≥1), hard:** empty / broken geometry / missing CRS /
      sentinel-saturated column → ``ModelRetry`` once (a real, deterministic defect).
    - **Advisory (level ≥2 visual, ≥3 redundancy), soft:** a second-modality opinion
      on a *structurally clean* result → appended as a note, never a retry (the
      verdict is subjective — ``doc/visual-validation.md`` §7).
    """
    from pydantic_ai import ModelRetry
    from selmakit import tool_returns

    async def validate_result(ctx, output):  # noqa: C901
        # C901 exception: as make_validation_gate — level logic
        # Only plain-text answers are gated; a DeferredToolRequests output (an
        # approval-gated tool call) is not a final result to validate.
        if not isinstance(output, str):
            return output

        level = _read_level(sessions_dir, getattr(ctx, "deps", None))
        if level < 1:
            return output

        advisory: list[str] = []

        # Files the answer claims but that don't exist — "claimed but never
        # produced". Needs only the answer text, so it runs even when the run wrote
        # nothing at all (the phantom-file case). Advisory for now; to make it a hard
        # ModelRetry instead, move this finding into the structural tier below.
        absent = _absent_claims(output, workspace)
        if absent:
            advisory.append(
                "reported file(s) not found on disk — "
                + ", ".join(f"`{n}`" for n in absent)
                + " (the result may not have been produced)"
            )

        paths = [p for p in _candidate_paths(tool_returns(ctx), workspace) if _mentioned(p, output)]

        if paths:
            # ── Tier 1: the hard structural floor (all levels ≥1) ──
            problems: list[tuple[str, str]] = [
                (path, msg) for path in paths for msg in _structural_problems(path)
            ]
            if problems:
                detail = _format_problems(problems)
                if _may_retry(ctx):
                    raise ModelRetry(
                        f"Result validation (level {level}) found a structural defect in the "
                        f"dataset(s) you reported:\n{detail}\n\n"
                        "This usually means a wrong extent, a failed filter/clip, or a missing "
                        "reprojection — not just a labelling issue. Investigate the step that "
                        "produced the file and fix it, then report the corrected result."
                    )
                return (
                    f"{output}\n\n> ⚠️ Validation note (level {level}): a structural check still "
                    f"flags {detail.replace(chr(10), ' ')} — treat this result with caution."
                )

            # ── Tier 1b: does the reported area hold the area it claims? ──
            # Structurally clean and still the wrong answer — the one defect class
            # that survives every level-1 check. Retries like the structural tier
            # (once, budget-aware), but asks for a justification rather than a fix:
            # the file may be right and only badly named, and only the model knows.
            identity = [(path, msg) for path in paths for msg in _area_identity_problems(path)]
            if identity:
                detail = _format_problems(identity)
                if _may_retry(ctx):
                    raise ModelRetry(
                        f"Result validation (level {level}) — check which area you actually "
                        f"used:\n{detail}\n\n"
                        "If this is the area the request asked for, say so in your answer and "
                        "name the source it came from. If it is a stand-in you picked because "
                        "the real one was hard to find (an OSM polygon that sounds similar, a "
                        "heritage or postal outline instead of an administrative one), fetch "
                        "the authoritative boundary — for an area below the Gemeinde that is "
                        "`geodata_search` → `wfs_features`, not OSM — and redo the count on it."
                    )
                advisory.append(f"area identity unresolved: {detail.replace(chr(10), ' ')}")

            # ── Tier 2: advisory second opinions on a structurally clean result ──
            # Only the first reported layer, to bound cost (doc §7: check the final
            # result, not every intermediate). Soft — a note, never a retry.
            primary = paths[0]
            if level >= 2 and vision_model:
                advisory += await asyncio.to_thread(
                    _visual_problems,
                    primary,
                    vision_model=vision_model,
                    base_url=base_url,
                    workspace=workspace,
                )
            if level >= 3:
                advisory += _redundancy_problems(primary)

        # ── Tier 1c: the answer rests on a bounding box ──
        # Last of the hard tiers on purpose: a broken file or a mislabelled area is
        # the worse defect and gets the one retry first. This one needs no reported
        # path — the run that found it named only its HTML map, so every path-based
        # check above was blind to it.
        bbox_problem = _bbox_extent_problem(tool_returns(ctx))
        if bbox_problem:
            if _may_retry(ctx):
                raise ModelRetry(
                    f"Result validation (level {level}) — check the extent you "
                    f"measured:\n- {bbox_problem}\n\n"
                    "If the task named an area (a city, Gemeinde, Kreis), redo it on the "
                    "boundary: re-fetch with `place=\"<Name>, <Land>, <Country>\"`, or clip "
                    "the layer against the polygon from `geocode(query, "
                    "output_path=...)` with `qgis_clip` — both in the same metric CRS — "
                    "and report the corrected figure. If the rectangle is what the "
                    "request actually wanted (an explicit coordinate window, a radius "
                    "around a point, a 'nearest X' question that must look past the "
                    "border), say so in your answer and keep your result."
                )
            advisory.append(f"extent unresolved: {bbox_problem}")

        # ── Tier 1d: the answer points at a rendered view without its exact path ──
        # Last of the hard tiers: this is the mildest defect of them all — the result
        # is right, only unreachable — so every worse finding gets the single retry
        # first. It earns a retry nonetheless because it is the one case that is
        # fixable **without a single tool call**: re-emit the same answer with the
        # path pasted in. As an advisory note it could not work at all — the note is
        # appended to the answer and never reaches the model (measured 2026-09-01:
        # four runs, four misses, with the rule spelled out in the instructions).
        unquoted = _unquoted_view_paths(tool_returns(ctx), output, workspace)
        if unquoted:
            listed = ", ".join(f"`{p}`" for p in unquoted)
            # If a dead link target triggered this, quote it verbatim: the model
            # must see that it wrote its own description of the path into the
            # brackets, not the path.
            dead = _dead_link_targets(output, workspace)
            placeholder = (
                f" Your answer links to `{dead[0]}`, which is not a file — that is "
                "the wording of the instruction, not the path it asked for."
                if dead else ""
            )
            if _may_retry_answer_only(ctx):
                raise ModelRetry(
                    f"Result validation (level {level}) — your answer points at a "
                    f"rendered view but not by its path, so nothing can open it: "
                    f"{listed}.{placeholder}\n\n"
                    "Repeat your answer unchanged except for this: write that "
                    "**absolute** path verbatim, on its own, with no markdown "
                    "emphasis around it and no placeholder text in front of it. The "
                    "dashboard matches the literal path and checks it on disk — a "
                    "bare filename resolves against the wrong directory and misses."
                )
            advisory.append(
                "a rendered view is named without its exact path, so it cannot be "
                f"embedded — quote it verbatim: {listed}"
            )

        if advisory:
            note = "; ".join(advisory)
            return (
                f"{output}\n\n> 🔎 Validation note (level {level}, advisory) — {note}. "
                "Re-check the step, or ignore it if the result is right."
            )
        return output

    return validate_result
