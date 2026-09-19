"""The validation gate's checks — pure, usable with or without an agent.

Design: `doc/validation-concept.md` §4.1/§6 (V3). The checks read facts from
``chester.geofacts`` and return findings; they never raise into a model loop. Two
callers use them:

* ``chester.runtime.gatehook`` (package chester-runtime) turns them into an enforced loop
  phase — a pydantic-ai ``output_validator`` that raises ``ModelRetry`` once per
  defect, reads the session's strictness level via SelmaKit, and adds the level-2
  visual check (which needs a vision model and the map renderer).
* ``inspect_result`` below offers the same checks as a fact-finder without force —
  what Chester-MCP serves as ``validate_result``.

Split out of the former single ``gate.py`` on 2026-09-19, when the repository became
four packages: the checks sit in chester-geo-tools, which imports nothing upward, so
Chester-MCP can use them without pulling in the agent stack. Before that split this
module was the one documented exception to "pure cores stay pure".

Levels are cumulative (level *n* runs the checks of 1…*n*): 0 off, 1 structural,
2 +visual, 3 +redundancy.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterator

from chester.geofacts import (
    RASTER_EXTS,
    attribute_facts,
    column_values,
    is_raster,
    raster_degenerate,
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
#: Words that name a *kind* of feature, not a place — German and English, folded as
#: `_name_tokens` folds them. A single area called "Flurstück" says what it is, not
#: where; it makes no claim a file name could contradict. Found 2026-09-19 in the first
#: chester-team runs: `team_parcel_metric.gpkg` holding one feature named "Flurstück"
#: was flagged as "may not be the same place" — "parcel" and "Flurstück" share no
#: word, because they are one word in two languages. The hard retry that followed sent
#: the orchestrator on a long detour. Used on **both** sides, like the stem list.
_FEATURE_CLASS_TOKENS = {
    "flurstueck", "flurstuecke", "grundstueck", "grundstuecke", "parzelle", "parzellen",
    "parcel", "parcels", "plot", "plots", "gebaeude", "building", "buildings",
    "gemeinde", "gemeinden", "municipality", "landkreis", "kreis", "county", "stadt",
    "city", "stadtteil", "ortsteil", "stadtbezirk", "bundesland", "state", "land",
    "feature", "features", "objekt", "object", "flaeche", "flaechen",
}
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


def inspect_result(paths: list[str], answer: str = "", *,
                   workspace: str = DEFAULT_WORKSPACE,
                   level: int = DEFAULT_LEVEL) -> dict:
    """Run the gate's checks **without an agent** and report what they found.

    The enforcing gate (`make_validation_gate`) lives inside a pydantic-ai run: it
    reads the transcript, raises `ModelRetry` and thereby *makes* the loop go round
    again. Over MCP none of that exists — a foreign client has no retry we can
    trigger. So the same checks are offered as a fact-finder: same findings, no
    force. **That loss of enforcement is the measured object of the F+MCP cell, not
    a shortcoming of this function** (`internal/chester-mcp.md` §5, Variante 3).

    Two kinds of check run here. Per produced file: structure (empty layer, missing
    CRS, geometry that does not match the declared type), index ranges, a stored
    area/length column against the real geometry, and redundancy. Over the answer
    text: links that point nowhere, and claims about files that do not exist.

    Deliberately absent, because they need the transcript rather than the result:
    the unquoted-path check and the bbox-extent check. Named here rather than
    silently skipped — `checks_not_run` says so in the return value too.

    ``must_fix`` is the machine-readable verdict. Without it every client would have
    to read prose to learn whether anything is wrong.
    """
    level = clamp_level(level)
    findings: list[dict] = []
    checked: list[str] = []
    for raw in paths:
        path = resolve_path(raw, workspace)
        if not Path(path).exists():
            findings.append({"path": raw, "check": "exists", "severity": "must_fix",
                             "problem": "no file at this path"})
            continue
        checked.append(raw)
        for check, problems in (
            ("structure", _structural_problems(path)),
            ("index_range", _index_range_problems(path)),
        ):
            findings += [{"path": raw, "check": check, "severity": "must_fix",
                          "problem": p} for p in problems]
        if level >= 3:  # noqa: PLR2004  # Stufe 3 = die beratenden Querprüfungen
            for check, problems in (
                ("area_identity", _area_identity_problems(path)),
                ("redundancy", _redundancy_problems(path)),
            ):
                findings += [{"path": raw, "check": check, "severity": "advisory",
                              "problem": p} for p in problems]

    if answer:
        findings += [{"path": t, "check": "dead_link", "severity": "must_fix",
                      "problem": "the answer links to a file that does not exist"}
                     for t in _dead_link_targets(answer, workspace)]
        findings += [{"path": c, "check": "absent_claim", "severity": "must_fix",
                      "problem": "the answer speaks about a file that does not exist"}
                     for c in _absent_claims(answer, workspace)]

    must_fix = any(f["severity"] == "must_fix" for f in findings)
    return {
        "ok": True,                      # the check ran; `must_fix` is the verdict
        "must_fix": must_fix,
        "findings": findings,
        "checked": checked,
        "level": level,
        "level_meaning": level_description(level),
        "checks_not_run": ["unquoted_view_paths", "bbox_extent", "visual"],
        "enforced": False,
    }


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


def _unquoted_view_paths(tool_results: list[tuple[str, Any]], answer: str,
                         workspace: str) -> list[str]:
    """Rendered HTML views the answer *refers to* without the exact path.

    The dashboard embeds a map or 3D view only when the **absolute** path the tool
    returned appears verbatim in the reply: SelmaKit's dashboard scans the text with
    ``(?:file://)?(/?[\\w./\\-]+\\.html)\\b`` and then calls ``os.path.isfile`` on the
    match — with no workspace resolution. A bare basename is therefore checked
    against the dashboard's own working directory and silently misses.

    Measured on 2026-09-01, four runs, four misses, none of them a broken result:
    ``map_contours_5m.html`` (basename only), ``_path_to_tegernheim_topo_map.html``
    and ``_instruction_output_path_tegernheim_contours.html`` (the model's own
    placeholder idiom, no such string anywhere in the 43k-character system prompt),
    and ``_kramgasse_tiny_3d.html_`` — the right name in markdown italics, where the
    trailing underscore already kills the regex's ``\\b``. The instruction for this
    is explicit and names the consequence; it did not bind. So the finding belongs in
    the return channel, which is what moved behaviour before (the bbox ``warning``).

    Fires only when the answer **does** point at the file (``_mentioned``) but no
    spelling in it reaches the file: an intermediate view the agent never mentions is
    not a defect, and neither is a *working* relative path. The consumer decides that,
    so the check **runs the consumer's own rule** rather than demanding one spelling.
    That distinction is not academic — the two renderers disagree about what they
    return (``render_map`` an absolute path, ``render_buildings_3d`` a workspace-
    relative one), so a rule of "must contain the absolute path" would flag a model
    that quoted its tool exactly.
    """
    seen: set[str] = set()
    out: list[str] = []
    # The dashboard's own matcher (selmakit.dashboard.app), deliberately duplicated:
    # this check is only worth anything as long as it asks what the consumer asks.
    reachable = {os.path.realpath(m.group(1))
                 for m in re.finditer(r"(?:file://)?(/?[\w./\-]+\.html)\b", answer,
                                      re.IGNORECASE)
                 if os.path.isfile(m.group(1))}
    # Zweiter Auslöser neben `_mentioned`: Die Antwort verlinkt etwas, das es nicht
    # gibt. Dann ist die gerenderte Ansicht gemeint, auch wenn ihr Name nirgends
    # steht — der Platzhalter-Fall vom 2026-09-07.
    dead = _dead_link_targets(answer, workspace)
    for _tool_name, content in tool_results:
        for s in _iter_strings(content):
            if len(s) > _MAX_PATH_LEN or Path(s).suffix.lower() != ".html":
                continue
            resolved = resolve_path(s, workspace)
            if resolved in seen:
                continue
            seen.add(resolved)
            if (os.path.isfile(resolved) and _mentioned(resolved, answer)
                    and os.path.realpath(resolved) not in reachable):
                out.append(resolved)
    if not out and dead:
        # Nur die zuletzt erzeugte Ansicht: Ein Lauf kann mehrere Karten schreiben,
        # gemeint ist die, auf die der tote Link zeigen sollte.
        views = [resolve_path(s, workspace) for s in seen
                 if os.path.isfile(resolve_path(s, workspace))]
        if views:
            out.append(max(views, key=os.path.getmtime))
    return out


#: Was in einer Antwort überhaupt verlinkt wird: die Ausgaben, die man ansehen kann.
_LINKABLE_EXTS = {".html", ".htm", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
                  ".gpkg", ".geojson", ".csv", ".pdf", ".md"}


#: Markdown-Linkziele, die nirgendwohin führen. Zwei Formen zählen, und nur zwei,
#: damit ein Weblink ohne Schema (`www.openstreetmap.org`) nicht mitgefangen wird:
#: ein Ziel mit einer von Chesters Ausgabe-Endungen, und ein Ziel **ganz ohne Punkt**
#: — denn ein Dateiname ohne Endung ist keiner, und genau so sieht der Platzhalter aus.
_MD_LINK_RE = re.compile(r"\[[^\]\n]*\]\(\s*([^)\s]+)\s*\)")


def _dead_link_targets(answer: str, workspace: str) -> list[str]:
    """Markdown-Linkziele in der Antwort, die auf keine existierende Datei zeigen.

    Der Fall, der das ausgelöst hat, gemessen 2026-09-07 (`dop-aerial-regensburg`):
    die Antwort endete mit ``[Regensburger Altstadt Luftbild](_the_absolute_path_from
    _the_tool_call_)``. Die Instruktion enthält diesen Platzhalter nirgends — sie
    *beschreibt* den Pfad in Prosa („the exact `output` path that render_map
    returned"), und das Modell hat die Beschreibung in die Klammer geschrieben. Über
    75 Bank-Läufe viermal, in drei verschiedenen Wortlauten (`_remote_path_to_map_`,
    `_path_to_map_file_`), also kein verunglückter Einzelfall, sondern die Form
    „setze hier X ein", die gelegentlich als Text gelesen wird.

    `_unquoted_view_paths` allein greift hier nicht: dessen Auslöser ist `_mentioned`,
    und der Platzhalter nennt weder Basisnamen noch Stamm der Datei. Ein totes
    Linkziel ist aber für sich schon eindeutig — der Text *will* verlinken und
    verlinkt ins Nichts.
    """
    dead: list[str] = []
    for m in _MD_LINK_RE.finditer(answer):
        target = m.group(1)
        if "://" in target or target.startswith(("#", "mailto:")):
            continue
        if len(target) > _MAX_PATH_LEN:
            continue
        suffix = Path(target).suffix.lower()
        looks_local = suffix in _LINKABLE_EXTS or "." not in target
        if looks_local and not os.path.isfile(resolve_path(target, workspace)):
            dead.append(target)
    return dead



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

    A file counts as absent only when **no** mention of it resolves to a real file.
    Measured 2026-09-05 (`street-buildings-then-refine`, step 1): the first attempt
    wrote the link as ``(_Users/…/lappersdorf_map.html)`` — an underscore where the
    leading slash belonged — and after the gate said so, the agent appended the
    corrected absolute path to the same reply. Judging the first spelling and
    skipping every later one (the old ``seen`` shortcut) reported a file as missing
    that existed and was correctly linked three lines further down. The claim this
    check makes is "not produced"; that claim is false as soon as one spelling
    resolves.
    """
    # Drop any scheme URL first (http/https/file/wms service links) so a filename
    # embedded in a URL — e.g. https://example.org/data.tif — isn't read as a local
    # output claim. The regex's char class excludes ':', so it would otherwise match
    # the tail of the URL past the scheme.
    text = re.sub(r"\w+://\S+", " ", answer)
    # Basisname → ob **irgendeine** Nennung auf eine Datei zeigt. dict statt set,
    # weil die Reihenfolge der ersten Nennung die Reihenfolge der Meldung bleibt.
    found: dict[str, bool] = {}
    for m in _OUTPUT_CLAIM_RE.finditer(text):
        token = m.group(0)
        if len(token) > _MAX_PATH_LEN:
            continue
        name = Path(token).name
        if found.get(name):
            continue
        found[name] = os.path.isfile(resolve_path(token, workspace))
    return [name for name, ok in found.items() if not ok]


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
            # Ein Raster ohne jede Variation ist eine schwarze Fläche, kein Ergebnis —
            # der Fall vom 2026-08-27 (266 MB, jedes Pixel 0, kein CRS, als Karte
            # gemeldet; der Nutzer sah es, jede Prüfung war zufrieden).
            flat = raster_degenerate(path)
            return crs_problem + ([flat] if flat else []) + _index_range_problems(path)
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
    stem_tokens = _name_tokens(Path(path).stem) - _GENERIC_STEM_TOKENS - _FEATURE_CLASS_TOKENS
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
        # Only the place-like part of the name is a claim ("Stadt Regensburg" →
        # "regensburg"); a name that is nothing but a kind of feature claims no place.
        name_tokens = _name_tokens(values[0]) - _GENERIC_STEM_TOKENS - _FEATURE_CLASS_TOKENS
        if name_tokens and not (name_tokens & stem_tokens):
            return [
                f"holds one area named '{values[0]}', which shares no word with the "
                f"file name '{Path(path).stem}' — the area you report and the area "
                f"in the file may not be the same place"
            ]
        return []  # the first populated name column decides
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
