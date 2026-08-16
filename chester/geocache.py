"""The GeoCache — a disk-reconciled, self-bounding inventory of local datasets.

This is the deterministic core behind ``GeoInventoryCapability`` (the agent
tools) and the ``data.py`` CLI. It has **no SelmaKit / pydantic-ai dependency**
so both can share it.

The store is a single human-readable Markdown table at
``<workspace>/geocache/geocache.md`` — one row per **dataset** (a multi-layer
container expands to one row per layer). Modelled on SelmaKit's ``SqliteMemory``
but without a database: the file *is* the state.

Two ideas keep it useful and bounded:

- **Awareness.** ``sync()`` reconciles disk ↔ inventory: it adds datasets that
  appeared, drops rows whose file is gone, and re-reads the facts (CRS, extent,
  counts) of everything present. Facts come from :mod:`chester.geofacts`
  (geopandas/rasterio/pyogrio header reads — cheap, no ``qgis_process``), so a
  full re-read every startup is fine; we never hash large rasters.

- **Expiry.** Every dataset carries ``created`` / ``last_used`` / ``ttl_days``
  → ``expires`` (counted from ``last_used``). ``sync()`` deletes datasets past
  expiry so the cache stays bounded; nothing is truly lost (downloads re-fetch,
  derived layers re-compute). Datasets classified ``source: user`` (under a
  read-only data root) **never** age.

Only the *non-derivable* columns are remembered across syncs —
``created`` / ``last_used`` / ``note`` / ``source`` (and a **pinned** TTL).
Everything else is re-derived from disk, so the inventory can't drift from
reality.

How long a dataset lives is resolved in this order:

1. a **pin** — a TTL set deliberately via ``note(ttl_days=…)``, written to the
   table as ``7*`` and remembered until re-pinned;
2. the provenance sidecar's ``ttl_days``, if the writing tool recorded one;
3. a **per-source override** (``ttl_by_source``), matched on the dataset's
   ``source`` exactly (``connector/osm``) or by a ``*`` family prefix
   (``connector/*``) — a bulky daily download can age out in a week while
   self-created results keep the default;
4. ``default_ttl_days``.

Steps 3–4 come from config, so they are *derived*, not remembered: an unpinned
TTL is recomputed on every sync and a config change takes effect immediately.
"""

from __future__ import annotations

# `builtins.list[...]` in this module is deliberate: the class has a public
# `list()` method, which shadows the builtin inside the class body and makes a
# plain `list[Dataset]` annotation unreadable to static tools. Renaming the
# method would change the API that data.py and the inventory tools call.
import builtins
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from chester import geoconfig, geofacts, provenance

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 30
# Marks a TTL that was set deliberately (``note(ttl_days=…)``) rather than
# derived from config, so a sync re-derives the latter but never the former.
PIN_MARKER = "*"
INVENTORY_NAME = "geocache.md"
GEOCACHE_DIRNAME = "geocache"

# Vector extensions worth inventorying. Rasters come from geofacts.RASTER_EXTS.
VECTOR_SCAN_EXTS = {
    ".geojson", ".gpkg", ".shp", ".sqlite", ".db", ".gml", ".kml", ".fgb", ".gpx",
}

_COLUMNS = [
    "dataset", "kind", "crs", "features", "extent (WGS84)", "size",
    "source", "created", "last_used", "ttl_days", "expires", "note",
]
_HEADER = (
    "# GeoCache inventory\n\n"
    "_Auto-managed by Chester (`geocache_sync`). One row per dataset; multi-layer\n"
    "containers expand to one row per layer. The **note** column is yours to edit\n"
    "(purpose/semantics); every other column is overwritten on the next sync.\n"
    "Datasets past **expires** are deleted on sync unless `source` is `user`._\n\n"
)


def _human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def _esc(text: str) -> str:
    """Make a value safe for a Markdown table cell (pipes/newlines)."""
    return text.replace("|", "/").replace("\n", " ").strip()


@dataclass
class Dataset:
    """One inventory row: disk-derived facts + remembered lifecycle fields."""

    key: str               # relative path, plus "::layer" for a container layer
    path: str              # workspace-relative file path
    abspath: str
    layer: str | None
    kind: str              # "vector" | "raster"
    crs: str | None
    is_geographic: bool
    geometry_type: str | None
    feature_count: int | None
    width: int | None
    height: int | None
    extent_wgs84: list[float] | None
    size_bytes: int
    source: str            # "user" | "chester" | "connector/*"
    created: str           # YYYY-MM-DD
    last_used: str         # YYYY-MM-DD
    ttl_days: int
    note: str = ""
    ttl_pinned: bool = False    # set via note(ttl_days=…); survives config changes
    query: str | None = None    # durable provenance (from the sidecar)
    licence: str | None = None

    @property
    def expires(self) -> str:
        if self.source == "user":
            return "never"
        d = date.fromisoformat(self.last_used) + timedelta(days=self.ttl_days)
        return d.isoformat()

    def is_expired(self, today: str | None = None) -> bool:
        if self.source == "user":
            return False
        today = today or date.today().isoformat()
        return date.fromisoformat(today) > date.fromisoformat(self.expires)

    def _features_cell(self) -> str:
        if self.kind == "raster":
            return f"{self.width}x{self.height}px"
        return str(self.feature_count if self.feature_count is not None else "?")

    def _kind_cell(self) -> str:
        return f"vector:{self.geometry_type or '?'}" if self.kind == "vector" else "raster"

    def _extent_cell(self) -> str:
        if not self.extent_wgs84:
            return "-"
        return ",".join(f"{v:.4f}" for v in self.extent_wgs84)

    def _ttl_cell(self) -> str:
        """The TTL, suffixed with ``*`` when it was pinned rather than derived."""
        return f"{self.ttl_days}{PIN_MARKER}" if self.ttl_pinned else str(self.ttl_days)

    def to_row(self) -> str:
        cells = [
            self.key, self._kind_cell(), self.crs or "-", self._features_cell(),
            self._extent_cell(), _human_size(self.size_bytes), self.source,
            self.created, self.last_used, self._ttl_cell(), self.expires,
            _esc(self.note),
        ]
        return "| " + " | ".join(_esc(str(c)) for c in cells) + " |"

    @property
    def crs_warning(self) -> str | None:
        """Flag datasets that can't be measured/aligned safely as they stand."""
        if self.crs is None:
            return "no CRS defined — measurements and overlays are unreliable"
        if self.is_geographic:
            return "geographic CRS (degrees) — reproject before measuring area/distance"
        return None

    def to_dict(self) -> dict:
        return {
            "dataset": self.key, "path": self.path, "layer": self.layer,
            "kind": self.kind, "geometry_type": self.geometry_type,
            "crs": self.crs, "crs_warning": self.crs_warning,
            "query": self.query, "licence": self.licence,
            "is_geographic": self.is_geographic, "features": self.feature_count,
            "size": [self.width, self.height] if self.kind == "raster" else None,
            "extent_wgs84": self.extent_wgs84, "size_bytes": self.size_bytes,
            "source": self.source, "created": self.created,
            "last_used": self.last_used, "ttl_days": self.ttl_days,
            "ttl_pinned": self.ttl_pinned,
            "expires": self.expires, "note": self.note,
        }


@dataclass
class GeoCache:
    """Inventory over a workspace's cached datasets.

    ``workspace`` is the dir scanned for geodata (Chester's
    ``.chester/workspace``); the inventory lives at
    ``<workspace>/geocache/geocache.md``. ``roots`` are read-only user data
    roots — datasets under them are tagged ``source: user`` and never expire.
    ``ttl_by_source`` overrides retention per ``source`` (see the module docstring).
    """

    workspace: str
    roots: list[str] = field(default_factory=list)
    default_ttl_days: int = DEFAULT_TTL_DAYS
    ttl_by_source: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(cls, workspace: str, geodata: dict | None = None) -> GeoCache:
        """Build a cache from the ``geodata`` config block (one source of truth).

        Used by the agent capability *and* by ``data.py``, so the CLI prunes on
        exactly the retention rules the agent works under. Pass an already-read
        ``geodata`` dict to avoid re-reading the config.
        """
        gd = geoconfig.load_geodata() if geodata is None else geodata
        return cls(
            workspace=workspace,
            roots=gd.get("roots") or [],
            default_ttl_days=gd.get("ttl_days") or DEFAULT_TTL_DAYS,
            ttl_by_source=gd.get("ttl_by_source") or {},
        )

    # ── paths ────────────────────────────────────────────────────────────────
    @property
    def geocache_dir(self) -> Path:
        return Path(self.workspace) / GEOCACHE_DIRNAME

    @property
    def inventory_path(self) -> Path:
        return self.geocache_dir / INVENTORY_NAME

    # ── public API ───────────────────────────────────────────────────────────
    def sync(self, today: str | None = None) -> dict:
        """Reconcile disk ↔ inventory and delete expired datasets.

        Returns a summary ``{added, refreshed, dropped, expired, total}``.
        """
        today = today or date.today().isoformat()
        remembered = self._parse_inventory()
        datasets = self._build_datasets(remembered, today)

        added = sorted(k for k in datasets if k not in remembered)
        refreshed = sorted(k for k in datasets if k in remembered)
        dropped = sorted(k for k in remembered if k not in datasets)

        kept, expired = self._apply_expiry(list(datasets.values()), today)
        self._write([kept[k] for k in sorted(kept)])
        return {
            "added": added,
            "refreshed": [k for k in refreshed if k in kept],
            "dropped": dropped,
            "expired": sorted(expired),
            "total": len(kept),
        }

    def list(self, filter: str | None = None, today: str | None = None) -> list[dict]:
        """Sync, then return inventory rows (optionally substring-filtered)."""
        self.sync(today=today)
        day = today or date.today().isoformat()
        rows = [d.to_dict()
                for d in self._build_datasets(self._parse_inventory(), day).values()]
        if filter:
            f = filter.lower()
            rows = [
                r for r in rows
                if f in r["dataset"].lower()
                or f in (r["note"] or "").lower()
                or f in (r["crs"] or "").lower()
                or f in r["kind"].lower()
            ]
        return sorted(rows, key=lambda r: r["dataset"])

    def note(self, path: str, note: str, layer: str | None = None,
             ttl_days: int | None = None, today: str | None = None) -> dict:
        """Attach a note to a dataset and optionally pin its TTL.

        Also touches ``last_used`` (a noted dataset is one you care about).
        Does not run expiry, so annotating never deletes anything.
        """
        today = today or date.today().isoformat()
        datasets = self._build_datasets(self._parse_inventory(), today)
        key = self._match_key(datasets, path, layer)
        if key is None:
            return {"ok": False, "error": f"no cached dataset matches '{path}'",
                    "known": sorted(datasets)}
        ds = datasets[key]
        ds.note = note
        ds.last_used = today
        if ttl_days is not None:
            # A deliberate pin: remembered across syncs, immune to config changes.
            ds.ttl_days = ttl_days
            ds.ttl_pinned = True
        self._write([datasets[k] for k in sorted(datasets)])
        return {"ok": True, "dataset": key, "note": ds.note,
                "ttl_days": ds.ttl_days, "ttl_pinned": ds.ttl_pinned,
                "expires": ds.expires}

    def touch(self, path: str, layer: str | None = None,
              today: str | None = None) -> bool:
        """Stamp ``last_used = today`` for a dataset (LRU; keeps it from expiring).

        A **cheap, in-place** edit of just the matching row(s)' ``last_used`` cell
        in ``geocache.md`` — no disk rescan, no inventory rewrite of facts — so it
        is safe to call from the read path of every tool that consumes a cached
        dataset (see ``resolve_path``). For a multi-layer container ``path``
        without a ``layer``, every layer of that container is touched. Returns
        whether any row changed.
        """
        today = today or date.today().isoformat()
        if not self.inventory_path.exists():
            return False
        idx = _COLUMNS.index("last_used")
        lines = self.inventory_path.read_text().splitlines()
        changed = False
        for i, line in enumerate(lines):
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) != len(_COLUMNS) or cells[0] == "dataset":
                continue
            if set(cells[0]) <= {"-", ":"}:  # separator row
                continue
            if not _key_matches(cells[0], path, layer):
                continue
            if cells[idx] != today:
                cells[idx] = today
                lines[i] = "| " + " | ".join(cells) + " |"
                changed = True
        if changed:
            self.inventory_path.write_text("\n".join(lines) + "\n")
        return changed

    def prune(self, dry_run: bool = False, today: str | None = None) -> dict:
        """Delete expired datasets (or, with ``dry_run``, just preview them).

        Real prune reuses ``sync`` (which already expires); the dry run lists what
        *would* go without touching disk. Backs the ``/geocache prune`` command.
        """
        today = today or date.today().isoformat()
        if dry_run:
            datasets = self._build_datasets(self._parse_inventory(), today)
            to_delete = self._files_to_expire(list(datasets.values()), today)
            expired = sorted(d.key for d in datasets.values() if d.abspath in to_delete)
            return {"ok": True, "dry_run": True, "deleted": False, "expired": expired}
        summary = self.sync(today=today)
        return {"ok": True, "dry_run": False, "deleted": True,
                "expired": summary["expired"]}

    def remove(self, dataset: str, today: str | None = None) -> dict:
        """Delete one cached dataset by name (refusing ``source: user``).

        Removing a multi-layer container drops all its layers (it's one file).
        Backs the ``/geocache rm`` command — a deliberate user action, not an
        agent tool, so Chester never deletes data autonomously.
        """
        today = today or date.today().isoformat()
        datasets = self._build_datasets(self._parse_inventory(), today)
        key = self._match_key(datasets, dataset, None)
        if key is None:
            return {"ok": False, "error": f"no cached dataset matches '{dataset}'",
                    "known": sorted(datasets)}
        ds = datasets[key]
        if ds.source == "user":
            return {"ok": False, "error": f"'{key}' is source: user (a referenced "
                    "data root) — refusing to delete; only cache copies can be removed"}
        _delete_dataset_file(ds.abspath)
        remaining = {k: d for k, d in datasets.items() if d.abspath != ds.abspath}
        self._write([remaining[k] for k in sorted(remaining)])
        dropped = sorted(k for k, d in datasets.items() if d.abspath == ds.abspath)
        return {"ok": True, "removed": dropped, "path": ds.path}

    def remove_all(self, dry_run: bool = False, today: str | None = None) -> dict:
        """Delete **every** cached dataset (``source != "user"``); dry_run previews.

        Syncs first so disk and inventory agree, then deletes each cache file (and
        its sidecar). Referenced ``source: user`` data-root datasets are **kept** —
        they are not Chester's to delete. Backs ``/geocache rm all``.
        """
        today = today or date.today().isoformat()
        self.sync(today=today)
        datasets = self._build_datasets(self._parse_inventory(), today)
        removable = {k: d for k, d in datasets.items() if d.source != "user"}
        kept = {k: d for k, d in datasets.items() if d.source == "user"}
        if not dry_run:
            for abspath in {d.abspath for d in removable.values()}:
                _delete_dataset_file(abspath)
            self._write([kept[k] for k in sorted(kept)])
        return {"ok": True, "dry_run": dry_run, "deleted": not dry_run,
                "removed": sorted(removable), "kept": sorted(kept)}

    def summary_lines(self, limit: int = 12,
                      today: str | None = None) -> builtins.list[str]:
        """Compact one-line-per-dataset summaries (most-recent first) for prompts."""
        datasets = self._build_datasets(self._parse_inventory(), today or date.today().isoformat())
        ordered = sorted(datasets.values(), key=lambda d: d.last_used, reverse=True)
        out = []
        for d in ordered[:limit]:
            extent = d._extent_cell()
            facts = f"{d._kind_cell()}, {d.crs or 'no CRS'}, {d._features_cell()}"
            note = f" — {d.note}" if d.note else ""
            out.append(f"{d.key}: {facts}, extent {extent}{note}")
        return out

    # ── internals ────────────────────────────────────────────────────────────
    def _ttl_for_source(self, source: str) -> int | None:
        """Retention override for a dataset's ``source``, or ``None`` if unset.

        An exact key (``connector/osm``) wins over a ``*`` family prefix
        (``connector/*``), and among competing prefixes the longest one wins —
        so a specific connector can always carve itself out of a family rule.
        """
        if not self.ttl_by_source:
            return None
        exact = self.ttl_by_source.get(source)
        if exact is not None:
            return int(exact)
        best_len, best = -1, None
        for pattern, days in self.ttl_by_source.items():
            if pattern.endswith(PIN_MARKER) and source.startswith(pattern[:-1]):
                if len(pattern) > best_len:
                    best_len, best = len(pattern), days
        return None if best is None else int(best)

    def _resolve_ttl(self, remembered: dict, meta: dict, source: str) -> tuple[int, bool]:
        """(ttl_days, pinned) for a dataset — see the module docstring's order.

        A pin is remembered; everything below it is derived from the sidecar or
        config on every sync, so changing config takes effect at once.
        """
        if remembered.get("ttl_pinned") and "ttl_days" in remembered:
            return int(remembered["ttl_days"]), True
        if "ttl_days" in meta:
            return int(meta["ttl_days"]), False
        override = self._ttl_for_source(source)
        return (self.default_ttl_days if override is None else override), False

    def _classify_source(self, abspath: str) -> str:
        """Source class for a dataset: data root → ``user``, else ``chester``.

        Used only as a fallback when there is no provenance sidecar (a sidecar's
        own ``source``, e.g. ``connector/osm``, takes precedence in
        ``_build_datasets``).
        """
        for root in self.roots:
            root_abs = os.path.abspath(os.path.expanduser(root))
            try:
                if os.path.commonpath([root_abs, abspath]) == root_abs:
                    return "user"
            except ValueError:  # different drives
                continue
        return "chester"

    def _scan_files(self) -> builtins.list[str]:
        """Geodata files under the workspace (and any configured roots)."""
        exts = VECTOR_SCAN_EXTS | geofacts.RASTER_EXTS
        seen: list[str] = []
        roots = [self.workspace, *self.roots]
        for base in roots:
            base = os.path.abspath(os.path.expanduser(base))
            if not os.path.isdir(base):
                continue
            for dirpath, dirs, files in os.walk(base):
                # Skip internal caches (e.g. `_lod2_tiles` — raw CityGML tiles that
                # aren't user datasets and can't be read as plain vector layers).
                dirs[:] = [d for d in dirs if not d.startswith("_")]
                for name in files:
                    if Path(name).suffix.lower() in exts:
                        seen.append(os.path.join(dirpath, name))
        return seen

    def _build_datasets(self, remembered: dict, today: str) -> dict[str, Dataset]:
        ws_abs = os.path.abspath(self.workspace)
        out: dict[str, Dataset] = {}
        for abspath in self._scan_files():
            try:
                rel = os.path.relpath(abspath, ws_abs)
            except ValueError:
                rel = abspath
            # Provenance is per file (one sidecar), shared by every layer it holds.
            meta = provenance.read_meta(abspath) or {}
            source = meta.get("source") or self._classify_source(abspath)
            meta_created = (meta.get("created_at") or "")[:10] or None

            multilayer = geofacts.is_multilayer_container(abspath)
            try:
                layers = geofacts.list_layers(abspath) if multilayer else [None]
            except Exception:  # noqa: BLE001 - unreadable container: skip
                continue
            for layer in layers:
                key = f"{rel}::{layer}" if layer else rel
                try:
                    facts = geofacts.dataset_facts(abspath, layer)
                except Exception:  # noqa: BLE001 - unreadable layer: skip
                    continue
                rem = remembered.get(key, {})
                geom = facts.get("geometry_types") or [None]
                ttl, pinned = self._resolve_ttl(rem, meta, source)
                out[key] = Dataset(
                    key=key, path=rel, abspath=abspath, layer=layer,
                    kind=facts["kind"], crs=facts.get("crs"),
                    is_geographic=bool(facts.get("is_geographic")),
                    geometry_type=geom[0],
                    feature_count=facts.get("feature_count"),
                    width=facts.get("width"), height=facts.get("height"),
                    extent_wgs84=facts.get("bounds_wgs84"),
                    size_bytes=facts.get("size_bytes", 0),
                    source=source,
                    created=rem.get("created") or meta_created or today,
                    last_used=rem.get("last_used", today),
                    ttl_days=ttl,
                    ttl_pinned=pinned,
                    note=rem.get("note", ""),
                    query=meta.get("query"),
                    licence=meta.get("licence"),
                )
        return out

    def _files_to_expire(self, datasets: builtins.list[Dataset],
                         today: str) -> set[str]:
        """File paths whose *every* dataset is expired (and not ``source: user``).

        A multi-layer container is only removed when all its layers are expired —
        we can't drop one layer without rewriting the container. One source of
        truth for both real expiry (``_apply_expiry``) and the dry-run preview
        (``prune(dry_run=True)``).
        """
        by_file: dict[str, list[Dataset]] = {}
        for d in datasets:
            by_file.setdefault(d.abspath, []).append(d)
        return {
            abspath for abspath, group in by_file.items()
            if all(d.is_expired(today) for d in group)
            and any(d.source != "user" for d in group)
        }

    def _apply_expiry(self, datasets: builtins.list[Dataset],
                      today: str) -> tuple[dict, builtins.list[str]]:
        """Delete expired files; return (kept datasets, expired keys)."""
        to_delete = self._files_to_expire(datasets, today)
        kept: dict[str, Dataset] = {}
        expired: list[str] = []
        for d in datasets:
            if d.abspath in to_delete:
                expired.append(d.key)
            else:
                kept[d.key] = d
        for abspath in to_delete:
            _delete_dataset_file(abspath)
        return kept, expired

    def _match_key(self, datasets: dict, path: str, layer: str | None) -> str | None:
        # exact key first
        if path in datasets:
            return path
        if layer and f"{path}::{layer}" in datasets:
            return f"{path}::{layer}"
        # match by basename / relpath suffix
        target = os.path.normpath(path)
        cands = [
            k for k, d in datasets.items()
            if d.path == target
            or os.path.basename(d.path) == os.path.basename(target)
            or k == target
        ]
        if layer:
            cands = [k for k in cands if datasets[k].layer == layer]
        return cands[0] if len(cands) == 1 else (cands[0] if cands else None)

    def _parse_inventory(self) -> dict[str, dict]:
        """Recover the remembered (non-derivable) columns, keyed by dataset."""
        if not self.inventory_path.exists():
            return {}
        remembered: dict[str, dict] = {}
        for line in self.inventory_path.read_text().splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) != len(_COLUMNS):
                continue
            if cells[0] in ("dataset", "---") or set(cells[0]) <= {"-", ":"}:
                continue
            row = dict(zip(_COLUMNS, cells))
            # A trailing "*" marks a deliberate pin; a bare number was derived
            # from config on an earlier sync and is re-derived now, so config
            # changes reach existing rows.
            raw_ttl = row["ttl_days"].strip()
            pinned = raw_ttl.endswith(PIN_MARKER)
            entry = {
                "created": row["created"],
                "last_used": row["last_used"],
                "note": "" if row["note"] == "-" else row["note"],
                "source": row["source"],
                "ttl_pinned": pinned,
            }
            if pinned:
                try:
                    entry["ttl_days"] = int(raw_ttl.rstrip(PIN_MARKER))
                except ValueError:  # unreadable pin: fall back to a derived TTL
                    entry["ttl_pinned"] = False
            remembered[row["dataset"]] = entry
        return remembered

    def _write(self, datasets: builtins.list[Dataset]) -> None:
        self.geocache_dir.mkdir(parents=True, exist_ok=True)
        head = "| " + " | ".join(_COLUMNS) + " |"
        sep = "| " + " | ".join("---" for _ in _COLUMNS) + " |"
        rows = [d.to_row() for d in datasets]
        body = "\n".join([head, sep, *rows]) if rows else "_empty_"
        self.inventory_path.write_text(_HEADER + body + "\n")


def start_periodic_sync(
    cache: GeoCache,
    interval_hours: float,
    *,
    on_result=None,
    initial_delay_hours: float | None = None,
) -> threading.Event:
    """Run ``cache.sync()`` every ``interval_hours`` on a daemon thread.

    Keeps a long-running gateway's cache bounded without waiting for the next
    restart: expiry only happens on sync, so a session left open for days would
    otherwise never prune. Returns the stop :class:`threading.Event` — set it to
    end the loop (the thread is a daemon, so process exit needs no cleanup).

    A **thread, not an asyncio task**, on purpose: ``sync()`` is blocking disk
    I/O (a header read per file), which on the gateway's event loop would stall
    every in-flight chat turn for the length of the scan. This also keeps the
    scheduler free of SelmaKit — the framework's own ``CronService`` fires *LLM
    turns*, which would be an absurd way to run a deterministic disk scan.

    An ``interval_hours`` of zero or less disables the loop; the returned event
    is then already set, so callers need no special case.
    """
    stop = threading.Event()
    interval = max(float(interval_hours or 0.0), 0.0) * 3600.0
    if interval <= 0:
        stop.set()
        return stop
    first = (interval if initial_delay_hours is None
             else max(float(initial_delay_hours), 0.0) * 3600.0)

    def _loop() -> None:
        delay = first
        # wait() returns True once stop is set — so this exits promptly.
        while not stop.wait(delay):
            delay = interval
            try:
                summary = cache.sync()
            except Exception:  # noqa: BLE001 - a failed scan must not kill the loop
                logger.exception("GeoCache background sync failed")
                continue
            if summary["added"] or summary["expired"] or summary["dropped"]:
                logger.info(
                    "GeoCache sync: +%d added, %d expired, %d dropped (%d total)",
                    len(summary["added"]), len(summary["expired"]),
                    len(summary["dropped"]), summary["total"],
                )
            if on_result is not None:
                try:
                    on_result(summary)
                except Exception:  # noqa: BLE001 - a bad callback must not kill the loop
                    logger.exception("GeoCache sync callback failed")

    threading.Thread(target=_loop, name="geocache-sync", daemon=True).start()
    logger.info("GeoCache background sync every %.2fh", interval / 3600.0)
    return stop


def _key_matches(key: str, path: str, layer: str | None) -> bool:
    """Does inventory ``key`` (``relpath`` or ``relpath::layer``) name ``path``?

    Matches on the full workspace-relative path, the bare key, or the basename
    (so a sloppy or absolute ``path`` still finds its row). When ``layer`` is
    given it must match the key's layer; when omitted, every layer of a container
    matches.
    """
    kpath, _, klayer_raw = key.partition("::")
    klayer: str | None = klayer_raw or None
    if layer is not None and klayer != layer:
        return False
    norm = os.path.normpath(path)
    return (
        kpath == norm
        or key == norm
        or os.path.basename(kpath) == os.path.basename(norm)
    )


def _delete_dataset_file(abspath: str) -> None:
    """Delete a dataset file plus its sidecars (shapefile parts, provenance meta)."""
    p = Path(abspath)
    targets = [p]
    if p.suffix.lower() == ".shp":  # shapefiles travel in a pack
        targets += [s for s in p.parent.glob(p.stem + ".*") if s != p]
    meta = Path(str(p) + ".meta.json")
    if meta.exists():
        targets.append(meta)
    for t in targets:
        try:
            t.unlink()
        except OSError:
            pass
