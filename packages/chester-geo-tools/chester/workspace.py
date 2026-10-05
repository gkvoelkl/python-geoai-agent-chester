"""Workspace-relative path resolution for Chester's tools.

The LLM is sloppy with paths: it may write ``.chester/workspace/x.tif``,
``chester/workspace/x.tif`` (dropped dot — observed in a real run),
``workspace/x.tif`` or just ``x.tif`` for the same intended file. Left alone these
land in different directories and later steps can't find them.

:func:`resolve_path` collapses all of those to **one** location, the GeoCache
working dir ``<workspace>/geocache/x.tif`` — the same place the inventory
(:mod:`chester.geocache`) manages and ages out (Phase 5.1: every tool *output*
is confined to the cache so a buffer around a user shapefile lands in the cache,
not next to read-only source data, and growth stays bounded).

Confinement is applied to inputs **and** outputs alike, which is what keeps
multi-step workflows consistent: a layer written in step 2 is found in step 3
regardless of how the model spells the path, because both resolve to the same
``geocache/`` location. Absolute paths and paths to an existing file (user-
provided source data, read in place) are left untouched; for back-compat a
relative name that already exists at the legacy ``<workspace>/`` root is still
found there.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_WORKSPACE = ".chester/workspace"
# The GeoCache working dir, relative to the workspace. Tool outputs land here
# (see chester/geocache.py, which inventories and expires its contents).
GEOCACHE_SUBDIR = "geocache"

# Leading directory prefixes the model invents that really mean "the workspace".
# Both the current (.chester) and legacy (.selmakit) names, with and without the
# leading dot, collapse to the configured workspace.
_WORKSPACE_ALIASES = (
    ".chester/workspace/",
    "chester/workspace/",
    ".selmakit/workspace/",
    "selmakit/workspace/",
    "workspace/",
)


def _strip_prefix(rel: str, prefix: str) -> str | None:
    """Strip ``prefix`` from ``rel`` **ignoring case**, or return ``None``.

    Case matters here because Chester's own instructions spell the cache
    ``GeoCache`` (`inventorytools`, `vectortools`: "put outputs in the GeoCache")
    while the directory is ``geocache``. Compared exactly, the model's own spelling
    missed every alias and built a tree inside the cache instead of collapsing into
    it: measured 2026-09-26 in the first Test-Level-2 team run, two probes wrote to
    ``geocache/GeoCache/…`` — right tools, right ressort, right numbers, and the
    result one directory below where every later step looks for it. The prefixes are
    all lower case, so lower-casing the candidate is the whole comparison.
    """
    return rel[len(prefix):] if rel.lower().startswith(prefix) else None


def resolve_path(  # noqa: C901
    path: str, workspace: str = DEFAULT_WORKSPACE, *, write: bool = False
) -> str:
# C901 exception: deliberately collects every path spelling of the model - each branch is
# an observed variant
    """Resolve ``path`` to a stable location under ``<workspace>/geocache/``.

    ``write=True`` marks an **output** and drops both read passthroughs: the result
    always lands in the cache, an absolute target reduced to its basename. Reading
    user data where it lies is a feature; writing there is not — an output outside
    the cache has no inventory entry, no touch-on-read protection and no TTL.

    *Measured 2026-09-13* (F+, `heldout-regensburg-danube-bridges`): the agent gave
    ``render_map`` an ``output_path`` of ``/tmp/donau_bruecken_regensburg_v2.html``; the
    map landed in ``/private/tmp/``, which macOS clears. Second damage: the gate checks
    datasets the run produced **and** the answer mentions — the answer named a path
    outside the cache, so it did not see the clipped layers and wrongly reported
    "extent unresolved", although ``vector_clip`` ran twice.

    - Absolute paths and paths that already exist (relative to the CWD) are
      returned unchanged **on reads** — user source data is read in place.
    - A leading workspace-ish prefix (``.chester/workspace/``, ``workspace/``,
      the legacy ``.selmakit`` forms) and an optional leading ``geocache/`` are
      stripped **whatever their capitalisation** — ``GeoCache/`` is the spelling
      Chester's own instructions use — so all spellings collapse together (no
      ``geocache/geocache/``, no ``geocache/GeoCache/``).
    - A relative name that already exists at the legacy ``<workspace>/`` root is
      returned there (back-compat); otherwise it resolves into ``geocache/`` and
      that parent directory is created so writes succeed.
    """
    expanded = os.path.expanduser(path)

    # A leading "/" in front of a workspace-ish prefix ("/workspace/x",
    # "/.chester/workspace/x", "/geocache/x") is the model spelling the
    # workspace, not a real root path — drop it so the alias match below fires.
    # A genuine absolute path to user data ("/Users/…/x.gpkg") matches no prefix
    # and falls through to the is_absolute() passthrough untouched.
    if expanded.startswith("/"):
        lead = expanded.lstrip("/")
        if any(
            _strip_prefix(lead, a) is not None
            for a in (*_WORKSPACE_ALIASES, GEOCACHE_SUBDIR + "/")
        ):
            expanded = lead
        elif not os.path.exists(expanded) and os.path.dirname(expanded.rstrip("/")) == "/":
            # A bare root-level path ("/buildings.geojson") is model sloppiness,
            # not a real target — writing to the filesystem root fails read-only.
            # A genuine absolute path to user data has a real parent dir and is
            # left alone by the is_absolute() passthrough below.
            expanded = lead

    p = Path(expanded)

    # Reads pass through: an absolute path, or one that already exists, is user source
    # data and is read where it lies. **Writes never do** — see the `write` parameter.
    if not write and (p.is_absolute() or p.exists()):
        return str(p)

    rel = expanded
    if write and (p.is_absolute() or ".." in p.parts):
        # Only the basename survives; a nested absolute path must not rebuild its tree
        # inside the cache, and an existing absolute file must not be overwritten in
        # place — that would be user source data.
        #
        # ``..`` belongs here for the same reason, and until 2026-09-14 it was the hole
        # in the lock: ``../../ausbruch.gpkg`` became ``<ws>/geocache/../../ausbruch.gpkg``
        # and demonstrably landed outside the workspace. Unlikely for Chester's own agent;
        # over the MCP server a **foreign** model sets this parameter, and the server
        # promises that outputs land in the cache. A promise that only holds for
        # well-meaning input is none.
        rel = p.name
    # Strip a leading ``./`` (the model writes ``./workspace/x``) so it doesn't
    # defeat the ``workspace/`` alias match below and mis-resolve into a nested
    # ``geocache/workspace/`` dir.
    while rel.startswith("./"):
        rel = rel[2:]
    for alias in _WORKSPACE_ALIASES:
        stripped = _strip_prefix(rel, alias)
        if stripped is not None:
            rel = stripped
            break
    stripped = _strip_prefix(rel, GEOCACHE_SUBDIR + "/")
    if stripped is not None:
        rel = stripped

    cache_target = Path(workspace) / GEOCACHE_SUBDIR / rel
    if cache_target.exists():  # an existing cache file → a read; mark it used (LRU)
        _touch_cache(workspace, f"{GEOCACHE_SUBDIR}/{rel}")
        return str(cache_target)
    root_target = Path(workspace) / rel
    if root_target.exists():  # legacy file written before confinement
        _touch_cache(workspace, rel)
        return str(root_target)

    cache_target.parent.mkdir(parents=True, exist_ok=True)
    return str(cache_target)


def _touch_cache(workspace: str, key: str) -> None:
    """Stamp ``last_used`` on a cached dataset being read (touch-on-read / LRU).

    Called from the read path so an actively-used dataset isn't pruned by a sync
    mid-workflow. Best-effort and lazily imported: it must never make path
    resolution fail or pull the GeoCache into a cycle. A fresh output (the file
    does not exist yet) never reaches here, so writes aren't touched.
    """
    try:
        from chester.geocache import GeoCache

        GeoCache(workspace=workspace).touch(key)
    except Exception:  # noqa: BLE001 - touch is advisory; resolution must not break
        pass
