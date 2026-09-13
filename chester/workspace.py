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


def resolve_path(  # noqa: C901
    path: str, workspace: str = DEFAULT_WORKSPACE, *, write: bool = False
) -> str:
# C901-Ausnahme: sammelt bewusst alle Pfadschreibweisen des Modells ein - jeder Zweig ist eine
# beobachtete Variante
    """Resolve ``path`` to a stable location under ``<workspace>/geocache/``.

    ``write=True`` marks an **output** and drops both read passthroughs: the result
    always lands in the cache, an absolute target reduced to its basename. Reading
    user data where it lies is a feature; writing there is not — an output outside
    the cache has no inventory entry, no touch-on-read protection and no TTL.

    *Gemessen 2026-09-13* (F+, `heldout-regensburg-danube-bridges`): Der Agent gab
    ``render_map`` ein ``output_path`` von ``/tmp/donau_bruecken_regensburg_v2.html``;
    die Karte landete in ``/private/tmp/``, das macOS wegräumt. Zweitschaden: Das Gate
    prüft Datensätze, die der Lauf erzeugt **und** die Antwort erwähnt — die Antwort
    nannte einen Pfad ausserhalb des Caches, also sah es die geschnittenen Ebenen nicht
    und meldete fälschlich „extent unresolved", obwohl ``vector_clip`` zweimal lief.

    - Absolute paths and paths that already exist (relative to the CWD) are
      returned unchanged **on reads** — user source data is read in place.
    - A leading workspace-ish prefix (``.chester/workspace/``, ``workspace/``,
      the legacy ``.selmakit`` forms) and an optional leading ``geocache/`` are
      stripped, so all spellings collapse together (no ``geocache/geocache/``).
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
            lead.startswith(a)
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
    if write and p.is_absolute():
        # Only the basename survives; a nested absolute path must not rebuild its tree
        # inside the cache, and an existing absolute file must not be overwritten in
        # place — that would be user source data.
        rel = p.name
    # Strip a leading ``./`` (the model writes ``./workspace/x``) so it doesn't
    # defeat the ``workspace/`` alias match below and mis-resolve into a nested
    # ``geocache/workspace/`` dir.
    while rel.startswith("./"):
        rel = rel[2:]
    for alias in _WORKSPACE_ALIASES:
        if rel.startswith(alias):
            rel = rel[len(alias) :]
            break
    if rel.startswith(GEOCACHE_SUBDIR + "/"):
        rel = rel[len(GEOCACHE_SUBDIR) + 1 :]

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
