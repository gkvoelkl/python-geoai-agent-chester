"""The enforcing validation gate — a result-based ``output_validator``.

Design: [`doc/validation-concept.md`](../doc/validation-concept.md) §4.1/§6 (V3).
Chester's validation was instruction-driven: the tools (`check_crs`,
`sanity_check_result`) existed, but nothing stopped the model from reporting an
unchecked result. This module turns the *level-1 structural floor* into an
enforced loop phase — a post-run validator registered via SelmaKit's
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

Levels are cumulative (level *n* runs the checks of 1…*n*). Levels 2 (visual) and
3 (redundancy) enforce the same level-1 floor today and call best-effort hooks
(`_visual_problems` / `_redundancy_problems`) that come online with V4/V5 — the
surface is stable, so raising a level gets stronger without an interface change
(§4.1 honesty note).

Standalone like ``geofacts``/``geocache`` — the only SelmaKit imports are the two
public helpers (``tool_returns``) and ``ModelRetry``; the checks read facts from
``chester.geofacts``.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Iterator

from chester.geofacts import (
    RASTER_EXTS,
    attribute_facts,
    column_values,
    is_raster,
    raster_facts,
    vector_facts,
)
from chester.workspace import DEFAULT_WORKSPACE, resolve_path

# Session-meta key holding the per-session strictness level (set by /valid_level).
VALID_LEVEL_KEY = "valid_level"
DEFAULT_LEVEL = 1
MIN_LEVEL = 0
MAX_LEVEL = 3

# Vector formats we can structurally check via geofacts (a real geopandas read).
# CityJSON (.json/.city.json) needs cjio, not vector_facts, so it is out of scope
# for the level-1 floor; plain .json is excluded to avoid non-spatial confusion.
_VECTOR_EXTS = {".gpkg", ".geojson", ".shp", ".gml", ".sqlite", ".db"}
_GEO_EXTS = _VECTOR_EXTS | RASTER_EXTS

# Output formats Chester *writes* — used to spot a file the answer claims to have
# produced. Deliberately narrower than an input allowlist: these are the extensions
# a produced result carries, so a name that ends in one and doesn't exist is a
# "claimed but never produced" artefact (the agent said it saved X, but no tool
# wrote it). A token containing "://" is a URL, not a local file.
_OUTPUT_CLAIM_RE = re.compile(r"[\w./+-]+\.(?:gpkg|geojson|tif|tiff|html|csv)\b", re.IGNORECASE)

# A path string is short; skip anything longer so a tool that echoes a big inline
# GeoJSON blob isn't parsed as a candidate path.
_MAX_PATH_LEN = 512
# Ignore very short stems when matching "mentioned in the answer" (a 2-3 char stem
# would false-match common words).
_MIN_STEM_LEN = 4

# The gate's placeholder set is stricter than geofacts' default: the empty string
# is excluded so the many legitimately-empty tag columns of an OSM export don't
# trigger a mandatory retry. What remains ("null"/"nan"/-9999 …) is a strong
# failed-join / leaked-nodata signal worth flagging.
_GATE_PLACEHOLDER_STRINGS = {"null", "none", "nan", "n/a", "#n/a"}

# ── area identity (V1b) ──
# Words a file stem uses to say what a layer *is*, not which area it holds. A stem
# built only from these ("clip_mask.gpkg") makes no claim to compare against.
_GENERIC_STEM_TOKENS = {
    "area",
    "areas",
    "bezirk",
    "bezirke",
    "boundary",
    "boundaries",
    "buffer",
    "clip",
    "clipped",
    "data",
    "district",
    "districts",
    "epsg",
    "final",
    "flaeche",
    "gebiet",
    "grenze",
    "grenzen",
    "layer",
    "mask",
    "merged",
    "metric",
    "output",
    "outline",
    "polygon",
    "polygons",
    "region",
    "reprojected",
    "result",
    "shape",
    "temp",
    "test",
    "tmp",
    "utm",
    "wgs84",
    "zone",
}
# Columns that carry a feature's own name across BKG, swissBOUNDARIES, OSM and WFS.
_NAME_COLUMNS = ("name", "gen", "bezeichnung", "bez", "title", "label", "gemeinde")
_MIN_NAME_TOKEN_LEN = 4
_UMLAUT_FOLD = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


_LEVELS = {
    0: "off — no validation; the answer passes through unchecked",
    1: "structural — empty result, invalid/null/empty geometry, missing CRS "
    "(in-process, deterministic). Default at start.",
    2: "structural + visual — renders the reported result and asks the configured "
    "vision model for a second opinion (advisory note; needs model.vision_model)",
    3: "structural + visual + redundancy — also cross-checks a stored area/length "
    "column against the geometry (advisory); deeper cross-checks via the "
    "cross_check tool / cross-check skill",
}


def level_description(level: int) -> str:
    """One-line meaning of a strictness level (shared by the gate and /valid_level)."""
    return _LEVELS.get(level, "unknown")


def levels_overview() -> str:
    """Markdown bullet list of all levels — the /valid_level help body."""
    return "\n".join(f"- `{k}` — {v}" for k, v in _LEVELS.items())


def clamp_level(value: Any) -> int:
    """Coerce a stored/typed level to a valid int in ``[MIN_LEVEL, MAX_LEVEL]``."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LEVEL
    return max(MIN_LEVEL, min(MAX_LEVEL, n))


def _iter_strings(obj: Any, _depth: int = 0) -> Iterator[str]:
    """Yield every string in a nested tool-result structure (dicts/lists/str)."""
    if _depth > 6:  # tool results are shallow; guard against pathological nesting
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v, _depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_strings(v, _depth + 1)


def _candidate_paths(tool_results: list[tuple[str, Any]], workspace: str) -> list[str]:
    """Existing geodata files referenced in this run's tool results (deduped).

    Walks every string value, keeps those with a geodata extension that resolve
    (via ``resolve_path``, so the model's path spellings collapse) to a real file.
    """
    seen: set[str] = set()
    out: list[str] = []
    for _tool_name, content in tool_results:
        for s in _iter_strings(content):
            if len(s) > _MAX_PATH_LEN or Path(s).suffix.lower() not in _GEO_EXTS:
                continue
            resolved = resolve_path(s, workspace)
            if resolved in seen:
                continue
            seen.add(resolved)
            if os.path.isfile(resolved):
                out.append(resolved)
    return out


def _mentioned(path: str, answer: str) -> bool:
    """True if the answer names this file (basename, or a long-enough stem)."""
    name = Path(path).name
    if name in answer:
        return True
    stem = Path(path).stem
    return len(stem) >= _MIN_STEM_LEN and stem in answer


def _absent_claims(answer: str, workspace: str) -> list[str]:
    """Output files the answer names that do **not** exist on disk.

    Catches "claimed but never produced": the agent says it saved a result to a
    file, but no tool actually wrote it. Scans the answer for tokens ending in an
    output extension Chester writes, resolves each (``resolve_path``), and returns
    the basenames of those that aren't a real file. URLs (``://``) are skipped.
    Deterministic and cheap — needs only the answer text, so it runs even when the
    run produced nothing at all (the phantom-file case).

    Conservative by construction: it only fires on Chester's own *output* extensions
    and only when the name resolves to no file, so a produced result (which exists)
    never trips it. The residual false positive — the answer naming a non-cached
    *source* file by its internal name — is rare in a user-facing reply.
    """
    # Drop any scheme URL first (http/https/file/wms service links) so a filename
    # embedded in a URL — e.g. https://example.org/data.tif — isn't read as a local
    # output claim. The regex's char class excludes ':', so it would otherwise match
    # the tail of the URL past the scheme.
    text = re.sub(r"\w+://\S+", " ", answer)
    seen: set[str] = set()
    out: list[str] = []
    for m in _OUTPUT_CLAIM_RE.finditer(text):
        token = m.group(0)
        if len(token) > _MAX_PATH_LEN:
            continue
        name = Path(token).name
        if name in seen:
            continue
        seen.add(name)
        if not os.path.isfile(resolve_path(token, workspace)):
            out.append(name)
    return out


# Normalised-difference indices are bounded to [-1, 1] by their own arithmetic —
# (a-b)/(a+b) cannot leave it for non-negative reflectances. SAVI/EVI are absent on
# purpose: their soil/atmosphere coefficients put them outside that range legally.
_BOUNDED_INDEX_NAMES = ("ndvi", "ndwi", "ndbi", "ndsi", "ndmi", "nbr")
_INDEX_TOL = 0.01  # float32 round-off, not a licence to be wrong
_INDEX_SAMPLE_PX = 512


def _index_range_problems(path: str) -> list[str]:
    """A named spectral index that leaves its own definition range is corrupt.

    From the `dop-ndvi-no-nir-bayern` run of 2026-08-25: `gdal:rastercalculator`
    evaluated `(B-A)/(B+A)` in the *input* dtype, so on uint16 Sentinel bands every
    pixel with NIR < Red underflowed — -52 wrapped to 65484 and came out as +81.
    Exactly the 795 negative-NDVI pixels (water, asphalt, roofs) turned into the
    highest "vegetation" in the scene, the Danube darkest of all. Every tool
    returned ok, and the map looked plausible until the colour bar read 10..60.

    Keyed on the file name because the corruption arrives precisely when the
    purpose-built `spectral_index` (which casts to float first) was *not* used, so
    there is no provenance record saying "ndvi" to key on instead.
    """
    stem = Path(path).stem.lower()
    kind = next((n for n in _BOUNDED_INDEX_NAMES if n in stem), None)
    if not kind:
        return []
    try:
        import rasterio
        from rasterio.enums import Resampling

        with rasterio.open(path) as src:
            shrink = max(src.width, src.height) / _INDEX_SAMPLE_PX
            out_h = int(src.height / shrink) if shrink > 1 else src.height
            out_w = int(src.width / shrink) if shrink > 1 else src.width
            # nearest, never average: averaging would blend a wrapped 65484 back
            # towards plausibility and hide the very defect being looked for.
            band = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.nearest,
                            masked=True)
        lo, hi = float(band.min()), float(band.max())
    except Exception:  # noqa: BLE001 - the range check is advisory, never fatal
        return []
    if lo < -1 - _INDEX_TOL or hi > 1 + _INDEX_TOL:
        return [
            f"{kind.upper()} values run {lo:.3f}..{hi:.3f}, outside the [-1, 1] the "
            f"index is defined on — integer bands underflowing in a raster "
            f"calculator do exactly this. Recompute with `spectral_index`, which "
            f"casts to float first."
        ]
    return []


def _structural_problems(path: str) -> list[str]:
    """Level-1 defects in a produced dataset (empty / broken geometry / no CRS).

    Deterministic and reference-free — the "not obviously broken" floor, not
    "correct". An unreadable file counts as a defect (the produced result can't be
    opened). Intent-dependent checks (e.g. measuring on a geographic CRS) are left
    to the ``check_crs`` instruction, since the gate can't know the user's intent
    without false positives.
    """
    try:
        if is_raster(path):
            f = raster_facts(path)
            crs_problem = [] if f["crs"] else ["no CRS defined (measurements unreliable)"]
            return crs_problem + _index_range_problems(path)
        f = vector_facts(path, full=True)
    except Exception as exc:  # noqa: BLE001 - an unreadable produced result is a defect
        return [f"unreadable ({type(exc).__name__})"]

    problems: list[str] = []
    if f["feature_count"] == 0:
        problems.append("empty result (0 features)")
    if not f["crs"]:
        problems.append("no CRS defined")
    if f.get("geom_invalid"):
        problems.append(f"{f['geom_invalid']} invalid geometr(ies)")
    if f.get("geom_null"):
        problems.append(f"{f['geom_null']} null geometr(ies)")
    if f.get("geom_empty"):
        problems.append(f"{f['geom_empty']} empty geometr(ies)")

    # V1: a column whose every populated value is a sentinel (all -9999 / all
    # "NULL") is a failed join or computation, not a real result. Strict set (no
    # empty string) keeps OSM tag columns from false-firing; best-effort read.
    try:
        af = attribute_facts(path, placeholder_strings=_GATE_PLACEHOLDER_STRINGS)
        saturated = [c for c, fc in af["fields"].items() if fc["all_placeholder"]]
        if saturated:
            shown = ", ".join(saturated[:3]) + ("…" if len(saturated) > 3 else "")
            problems.append(f"column(s) [{shown}] entirely placeholder/sentinel (failed join?)")
    except Exception:  # noqa: BLE001 - attribute facts are advisory
        pass
    return problems


def _name_tokens(text: str) -> set[str]:
    """Comparable word tokens: lowercased, umlauts folded, short words dropped."""
    folded = str(text).lower().translate(_UMLAUT_FOLD)
    return {t for t in re.split(r"[^a-z0-9]+", folded) if len(t) >= _MIN_NAME_TOKEN_LEN}


# The one sentence every bbox warning shares — `osm_features`, the vector-filter
# path and the GTFS window all phrase it differently around this core.
_BBOX_WARNING_MARKER = "a BBOX (a rectangle)"
# A later call that puts the extent right again. `qgis_clip` cuts to a boundary,
# `qgis_intersection` and `qgis_extract_by_location` select against one — after any
# of them the rectangle is gone and the warning is answered.
_EXTENT_HEALING_TOOLS = {"qgis_clip", "qgis_intersection", "qgis_extract_by_location"}
# Re-fetching through `place=` clips during download, so the same tool returning
# *without* the warning supersedes the bbox layer. Only tools that decide an
# **extent** belong here: `vector_filter` was in this set for one draft and swallowed
# the very run the check was built for — an attribute filter cannot heal a rectangle,
# it only removes rows inside it.
_AREA_FETCH_TOOLS = {"osm_features", "fetch_gtfs_stops"}


def _bbox_extent_problem(returns: list[tuple[str, Any]]) -> str | None:
    """A bbox warning was returned this run and nothing afterwards fixed the extent.

    The defect this catches, measured: asked for the cycleway kilometres *in
    Regensburg*, the agent pulled OSM by bounding box, read the warning that says a
    rectangle overcounts a named area — and reported 226,3 km anyway. Clipped to the
    city it is 175,9 km; 22 % of the answer lay outside Regensburg. The map even
    showed the boundary, drawn over paths that ran past it (2026-08-23).

    Why the gate has to be the one to notice: the warning sat in the tool's return
    value and was ignored, the file the answer named was an HTML map (so the
    path-based checks never saw a layer), and the judge called 226 km "a plausible
    order of magnitude" — it cannot see an extent. Three layers, none of them able.

    Reference-free like ``_area_identity_problems``: it compares the run's own two
    statements — "this came from a rectangle" and "here is your area result" — and
    needs no notion of which place was meant. A bbox that was *deliberate* is a
    legitimate answer, so the finding asks for a justification rather than a fix.
    """
    warned_at: int | None = None
    for i, (_name, content) in enumerate(returns):
        if _BBOX_WARNING_MARKER in _as_text(content):
            warned_at = i
    if warned_at is None:
        return None
    for name, content in returns[warned_at + 1 :]:
        if name in _EXTENT_HEALING_TOOLS:
            return None
        text = _as_text(content)
        if name in _AREA_FETCH_TOOLS and _BBOX_WARNING_MARKER not in text and '"ok": true' in text:
            return None
    return (
        "the data behind this answer came from a BOUNDING BOX and nothing afterwards "
        "clipped it to the area — a rectangle reaches into the neighbouring "
        "municipalities, so a total, a count or an average computed on it is too high "
        "and covers the wrong extent"
    )


def _as_text(content: Any) -> str:
    """A tool return as searchable text, whatever shape it came back in."""
    if isinstance(content, str):
        return content
    try:
        import json

        return json.dumps(content, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - a diagnostic must never break the run
        return str(content)


def _area_identity_problems(path: str) -> list[str]:
    """A single-feature area layer whose file name and whose own ``name`` disagree.

    The defect this catches, from a real run: asked for the *Stadtbezirk
    Innenstadt*, the agent wrote ``innenstadt_boundary.gpkg`` — holding the OSM
    relation "Altstadt von Regensburg mit Stadtamhof", the UNESCO world-heritage
    outline (``heritage=1``, 1.46 km²). Every count made inside it answers a
    different question than the one asked, and nothing downstream can notice: the
    layer is structurally perfect.

    Reference-free by construction, which is what lets it sit in the gate at all
    (see ``_structural_problems`` on intent): it compares the file's *own* two
    statements about itself. A stem with no place-like token claims nothing and is
    skipped, and any token overlap in either direction ("districts_innenstadt" ↔
    "Innenstadt", "welterbe_altstadt" ↔ "Altstadt von Regensburg…") stays silent —
    including the case where the heritage outline is exactly what was wanted.

    Only single-feature layers qualify: one polygon that carries a name *is* an
    area definition, while a 300-feature layer's names are data, not a claim.
    """
    try:
        f = vector_facts(path, full=True)
    except Exception:  # noqa: BLE001 - unreadable is the structural check's finding
        return []
    if f["feature_count"] != 1:
        return []
    stem_tokens = _name_tokens(Path(path).stem) - _GENERIC_STEM_TOKENS
    if not stem_tokens:
        return []

    columns = f.get("columns") or {}
    for column in columns:
        if column.lower() not in _NAME_COLUMNS:
            continue
        try:
            values = column_values(path, column, limit=1).get("values") or []
        except Exception:  # noqa: BLE001 - advisory facts never break the gate
            return []
        if not values:
            continue
        name_tokens = _name_tokens(values[0])
        if name_tokens and not (name_tokens & stem_tokens):
            return [
                f"holds one area named '{values[0]}', which shares no word with the "
                f"file name '{Path(path).stem}' — the area you report and the area "
                f"in the file may not be the same place"
            ]
        return []  # the first populated name column decides
    return []


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
        from chester.capabilities.mapoutput import _ask_vision_model, _render_snapshot
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


# Above this median relative gap, a stored area/length column is treated as
# disagreeing with the geometry (stale attribute / wrong units).
_AREA_CONSISTENCY_TOL = 0.10


def _redundancy_problems(path: str) -> list[str]:
    """Level-3 redundancy check (V5, advisory): the one cross-check that needs no
    external input — a stored ``area``/``length`` column vs the recomputed geometry
    (a two-method disagreement = a stale attribute or wrong units). Case-dependent
    redundancy (aggregate vs ``region_hierarchy`` parent, two-method heights) needs a
    second source the gate doesn't have — that stays the ``cross_check`` tool + the
    ``cross-check`` skill. Returns [] when there is no area/length column or no metric
    CRS. Advisory only (a note, never a retry)."""
    try:
        from chester.geofacts import area_length_consistency

        r = area_length_consistency(path)
    except Exception:  # noqa: BLE001 - redundancy is advisory
        return []
    if r and r["median_rel_diff"] > _AREA_CONSISTENCY_TOL:
        pct = round(r["median_rel_diff"] * 100)
        return [
            f"stored {r['kind']} column '{r['column']}' disagrees with the geometry by "
            f"~{pct}% (stale attribute or wrong units?)"
        ]
    return []


def _format_problems(problems: list[tuple[str, str]]) -> str:
    """Group ``(path, message)`` pairs into one bullet per file."""
    by_file: dict[str, list[str]] = {}
    for path, msg in problems:
        by_file.setdefault(Path(path).name, []).append(msg)
    return "\n".join(f"- `{name}`: {'; '.join(msgs)}" for name, msgs in by_file.items())


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
    # C901-Ausnahme: die Stufen 0-3 des Gates; jede Stufe ist ein Zweig, das ist der Entwurf
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
        # C901-Ausnahme: wie make_validation_gate: Stufenlogik
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
                retry = getattr(ctx, "retry", 0) or 0
                max_retries = getattr(ctx, "max_retries", 1)
                if max_retries is None:
                    max_retries = 1
                # Retry exactly once (the doc's "einmalig ModelRetry"), never exceeding
                # the framework's output-retry budget — a second failure would raise
                # instead of retrying, and looping a weak model is worse than a warning.
                if retry < min(1, max_retries):
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
                retry = getattr(ctx, "retry", 0) or 0
                max_retries = getattr(ctx, "max_retries", 1)
                if max_retries is None:
                    max_retries = 1
                if retry < min(1, max_retries):
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
            retry = getattr(ctx, "retry", 0) or 0
            max_retries = getattr(ctx, "max_retries", 1)
            if max_retries is None:
                max_retries = 1
            if retry < min(1, max_retries):
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

        if advisory:
            note = "; ".join(advisory)
            return (
                f"{output}\n\n> 🔎 Validation note (level {level}, advisory) — {note}. "
                "Re-check the step, or ignore it if the result is right."
            )
        return output

    return validate_result
