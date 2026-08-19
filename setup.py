"""Chester — project setup (config + workspace scaffolding).

Adapted from SelmaKit's reference ``setup.py``. Creates the ``.chester`` state
directory with a pre-filled ``chester.json`` and the workspace files that give
the agent its identity. Run once after cloning:

    uv run python setup.py

It is idempotent — existing files are kept (skipped), so it is also safe to call
on every gateway start. Chester's identity lives in ``SOUL.md`` / ``IDENTITY.md``
(injected by SelmaKit's WorkspacePromptCapability), which is why the agent needs
no hard-coded system prompt.
"""

import filecmp
import json
import shutil
from pathlib import Path

from rich import print

STATE_DIR = ".chester"
CONFIG_NAME = "chester.json"

# ── Default config (new SelmaKitConfig schema: channels nested) ──────────────

DEFAULT_CONFIG = {
    "model": {
        # Recommended local model: reliable tool calling + concise answers.
        # (On Apple silicon the gemma4:26b-mlx build is faster.) Swap via /model or here.
        "model": "ollama/gemma4:26b",
        "base_url": "http://localhost:11434/v1",
        # Key for a hosted model (openai/anthropic/google). Empty → the provider's
        # env var (OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY) is used.
        # Only needed when model.model (or a hosted evals.judge_model) is hosted.
        "api_key": "",
        # Fallback vision model for visual validation (inspect_map): used when the
        # main model reports it cannot see the snapshot. Empty disables the fallback.
        # Must be a *multimodal* model — the previous default named a text-only one,
        # so every scaffold shipped a visual check that 404'd on first use.
        "vision_model": "ollama/qwen3-vl:latest",
        "timeout_seconds": 640,
        "thinking": None,
    },
    "memory": {
        "enabled": True,
        "vector_search": False,
        "embed_model": "nomic-embed-text",
        "temporal_decay": False,
        "temporal_decay_rate": 0.01,
    },
    "channels": {
        "webchat": {"enabled": True, "host": "0.0.0.0", "port": 8000, "log_level": "info"},
        "telegram": {"enabled": False},
    },
    # Data layer (both holdings, Phase 5): read-only data roots that the GeoCache
    # catalogues in place (source: user, never pruned) and the connectors expose;
    # an optional PostGIS container connector. Empty/unset → those features are
    # inert. See doc/geodata-concept.md §4.
    "geodata": {
        "roots": [],
        "postgis": {"dsn": "", "schema": "public"},
        # Extra/override STAC catalogs merged over the built-ins (earth-search,
        # planetary-computer, cdse); each: {"url": ..., "sign": bool}.
        "stac_catalogs": {},
        # GeoCache retention. `ttl_days` is the default lifetime (counted from a
        # dataset's LAST USE, not its download); `ttl_by_source` overrides it per
        # provenance source — an exact name ("connector/osm") beats a family
        # prefix ("connector/*"), and among prefixes the longest wins. Use it to
        # age bulky re-fetchable downloads out quickly while keeping self-created
        # results ("chester") around. `source: user` data roots never expire, and
        # a TTL pinned via `geocache_note` (shown as `7*` in geocache.md) beats
        # all of this. Empty map → the default applies to everything.
        "ttl_days": 30,
        "ttl_by_source": {},
        # Re-sync the GeoCache every N hours while the gateway runs (0 = off).
        # Syncing already happens at startup and before every `geocache_list`,
        # which is enough for CLI use; this only matters for a gateway left up
        # for days, where nothing would otherwise trigger expiry. Runs on a
        # daemon thread, no LLM involved.
        "sync_interval_hours": 0,
        # Statistical connectors (Phase 5.8). Three credential-free sources are
        # wired (always on, no config): `eurostat` (EU/NUTS), `wikidata` (DE per-
        # Gemeinde), `worldbank` (global per-country). The German GENESIS-2020
        # sources (regionalstatistik / genesis / zensus2022) were removed — their
        # API required a per-machine account. This block is kept for future
        # authenticated sources; unused by the credential-free connectors.
        "statistics": {},
    },
    # Benchmark scoring (testprompt.py --judge). The judge model grades a run's
    # answer against the test's rubric; keep it INDEPENDENT of model.model (a model
    # grading itself is self-referential, which testprompt.py warns about). It must
    # emit the nested `Verdict` schema reliably — a too-small local model (e.g. a
    # 12B) fails that and exhausts its output retries, so pick a capable one. Default
    # to qwen3.5:27b (independent of the gemma4 agent, handles the schema); set
    # empty to force an explicit --judge-model, or a hosted model for best quality.
    "evals": {"judge_model": "ollama/qwen3.5:27b-mlx"},
    # Sub-agents (SelmaKit): isolated worker agents the main agent hands
    # self-contained work to via one `delegate_task` tool — each runs in its own
    # context (never sees the parent chat), so a long web-research loop doesn't
    # bloat the main geo agent's context. NOTE: SelmaKit gives sub-agents only
    # filesystem + web search/fetch (NOT Chester's QGIS/geo tools), so they are
    # research workers, not geoprocessors. Needs the `selmakit[subagents]` extra.
    "subagents": {
        "enabled": True,
        "agents": [
            {
                "name": "data-scout",
                "description": (
                    "Finds authoritative, ideally OPEN geodata sources on the web "
                    "(national/state mapping-agency portals, open-data catalogs, "
                    "OGC WFS/WCS/Atom services, direct file downloads) for a place "
                    "and data need. Returns a short ranked list of working URLs "
                    "with service type, format, CRS and licence. Delegate a deep "
                    "web data-hunt here to keep the main context clean."
                ),
                "system_prompt": (
                    "You are a geodata sourcing specialist. Given a place and a "
                    "data need (boundary, building footprints, terrain, a thematic "
                    "layer), find AUTHORITATIVE and ideally OPEN sources: national/"
                    "state mapping agencies, open-data catalogs (data.europa.eu, "
                    "regional GDI portals), OGC services (WFS/WCS/Atom) and direct "
                    "downloads. For each, give the working URL, service type (WFS "
                    "typename / Atom feed / direct GeoTIFF-GPKG-Shapefile / CKAN "
                    "resource), format, CRS if known, and licence (prefer DL-DE / "
                    "CC). Verify links resolve when you can. Answer concisely as a "
                    "short ranked list, not prose. If nothing open exists, say so "
                    "and name the closest restricted option."
                ),
                "max_calls": 12,
                "timeout_seconds": 180,
            },
            {
                "name": "researcher",
                "description": (
                    "General web research with sources — background facts, "
                    "definitions, method/context questions. Delegate a multi-search "
                    "lookup here instead of running many searches inline."
                ),
                "system_prompt": (
                    "You are a research assistant. Answer factual/background "
                    "questions using web search and fetch. Check multiple sources, "
                    "prefer primary/official ones, and answer concisely with the "
                    "key facts and their source URLs. State uncertainty explicitly."
                ),
                "max_calls": 8,
                "timeout_seconds": 120,
            },
        ],
    },
    "session": {"reset": {"at_hour": 4, "idle_minutes": None}},
    "heartbeat": {
        "enabled": False,
        "every": "30m",
        "active_hours": None,
        "timezone": "UTC",
        "target": "last",
        "isolated_session": False,
    },
}

# ── Default workspace files (Chester's identity, pre-filled) ─────────────────

DEFAULT_WORKSPACE_FILES = {
    "SOUL.md": """\
# SOUL.md — How Chester Works

You are Chester, a local Geo-AI agent. You turn natural-language geospatial
requests into real geoprocessing.

## Principles

**Correctness over plausibility.** Geodata results are objectively right or
wrong. Pick a sensible CRS, reproject before measuring area or distance, and
validate (CRS, area, plausibility) before reporting a result.

**Never fabricate data.** Do not invent, simulate, estimate as a proxy, or
hardcode values to fill a gap you could not fetch (e.g. a population you could
not download). A result built on made-up inputs is wrong, not approximate. If
authoritative data cannot be obtained, stop and report the blocker — name the
source you needed and why it failed — instead of producing a plausible-looking
number.

**Plan, then act.** Understand the request, sketch the tool chain, fix the CRS,
then run the steps: discover data (geocode / OSM / STAC), run QGIS algorithms,
analyse vectors, detect water/vegetation from imagery, then render a map.

**Try first, ask second.** Inspect the workspace, search the QGIS toolbox,
describe an algorithm's parameters — then ask only if you are genuinely stuck.

**Be traceable.** State which algorithms ran with which parameters, so a result
can be reproduced.

**Private things stay private.** Geodata stays on the machine; you run locally.

---
_This file is yours. Evolve it over time._
""",
    "IDENTITY.md": """\
Name: Chester
Role: Local Geo-AI agent (SelmaKit + QGIS + Ollama)
Vibe: Precise, practical, geo-savvy — explains the steps it took.
Emoji: 🌍
""",
    "USER.md": """\
User: (your name here)
Preferences: (area of interest, preferred CRS, default region, …)
""",
    "HEARTBEAT.md": """\
# HEARTBEAT.md

# Leave empty to skip heartbeat calls.
# Add tasks below when the agent should check something periodically.
""",
}


# ── Setup ────────────────────────────────────────────────────────────────────


def setup(
    state_dir: str = STATE_DIR, config_name: str = CONFIG_NAME, *, quiet: bool = False
) -> None:
    """Initialize the Chester state dir, config and workspace files.

    Idempotent. With ``quiet=True`` (used on gateway/CLI start) only newly
    created files are reported and the header/footer are suppressed, so a
    fully-initialized project produces no output; ``python setup.py`` runs
    verbose.
    """
    base = Path(".").resolve()
    chester_dir = base / state_dir
    config_path = chester_dir / config_name
    workspace_dir = chester_dir / "workspace"
    memory_dir = workspace_dir / "memory"
    skills_dst = workspace_dir / "skills"
    skills_src = base / "skills"

    if not quiet:
        print(f"[bold blue]Initializing Chester[/bold blue]\n[dim]Root: {base}[/dim]")

    _ensure_dir(chester_dir, base, quiet)

    if not config_path.exists():
        config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=4), encoding="utf-8")
        print(f"[green]✔[/green] Created config:    [cyan]{config_path.relative_to(base)}[/cyan]")
    elif not quiet:
        print(
            "[yellow]![/yellow] Config exists:     "
            f"[cyan]{config_path.relative_to(base)}[/cyan]  (skipped)"
        )

    _ensure_dir(workspace_dir, base, quiet)

    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_index = memory_dir / "MEMORY.md"
    if not memory_index.exists():
        memory_index.write_text("# Memory\n", encoding="utf-8")
        print("[green]✔[/green] Created:           [cyan]workspace/memory/MEMORY.md[/cyan]")
    elif not quiet:
        print(
            "[yellow]![/yellow] Already exists:    "
            "[cyan]workspace/memory/MEMORY.md[/cyan]  (skipped)"
        )

    _deploy_workspace_files(workspace_dir, quiet)

    skills_dst.mkdir(parents=True, exist_ok=True)
    _deploy_skills(skills_src, skills_dst, quiet)

    _sync_geocache(workspace_dir, quiet)

    if not quiet:
        print("\n[bold green]Setup complete.[/bold green]")
        print(
            "[dim]Edit [cyan].chester/chester.json[/cyan] to configure "
            "the model and channels.[/dim]"
        )
        print("[dim]Edit workspace files (SOUL.md, IDENTITY.md, USER.md) to tune the agent.[/dim]")


def _sync_geocache(workspace_dir: Path, quiet: bool) -> None:
    """Reconcile the GeoCache inventory with the workspace on disk.

    Runs on every (quiet) start so the agent boots with an accurate view of its
    cached datasets. Best-effort: an unreadable file must never break setup.
    """
    try:
        from chester.geocache import GeoCache

        summary = GeoCache(workspace=str(workspace_dir)).sync()
    except Exception as exc:  # noqa: BLE001 - setup must survive a bad cache file
        if not quiet:
            print(f"[yellow]![/yellow] GeoCache sync skipped: {exc}")
        return
    if not quiet:
        print(
            "[green]✔[/green] GeoCache synced:    "
            f"[cyan]{summary['total']} dataset(s)[/cyan] "
            f"(+{len(summary['added'])} added, "
            f"{len(summary['expired'])} expired, {len(summary['dropped'])} dropped)"
        )


def _ensure_dir(path: Path, base: Path, quiet: bool) -> None:
    rel = path.relative_to(base)
    if not path.exists():
        path.mkdir(parents=True)
        print(f"[green]✔[/green] Created directory: [cyan]{rel}[/cyan]")
    elif not quiet:
        print(f"[yellow]![/yellow] Already exists:    [cyan]{rel}[/cyan]  (skipped)")


def _deploy_workspace_files(workspace_dir: Path, quiet: bool) -> None:
    for name, content in DEFAULT_WORKSPACE_FILES.items():
        dest = workspace_dir / name
        if not dest.exists():
            dest.write_text(content, encoding="utf-8")
            print(f"[green]✔[/green] Created:           [cyan]workspace/{name}[/cyan]")
        elif not quiet:
            print(f"[yellow]![/yellow] Already exists:    [cyan]workspace/{name}[/cyan]  (skipped)")


def _deploy_skills(skills_src: Path, skills_dst: Path, quiet: bool) -> None:
    """Copy version-controlled skills from ``skills/`` into ``workspace/skills/``."""
    if not skills_src.exists():
        if not quiet:
            print("[dim]  No skills/ directory found — skipping skill deployment.[/dim]")
        return

    skill_dirs = sorted(d for d in skills_src.iterdir() if d.is_dir() and (d / "SKILL.md").exists())
    if not skill_dirs:
        if not quiet:
            print("[dim]  No skills found in skills/ — skipping.[/dim]")
        return

    copied = updated = skipped = 0
    for src_dir in skill_dirs:
        dst_dir = skills_dst / src_dir.name
        existed = (dst_dir / "SKILL.md").exists()  # before we touch it
        dst_dir.mkdir(parents=True, exist_ok=True)
        # Deploy a file when it is missing OR its content differs from the source,
        # so edits to a version-controlled skill are picked up (not just new ones).
        changed = [
            f
            for f in src_dir.iterdir()
            if f.is_file()
            and (
                not (dst_dir / f.name).exists()
                or not filecmp.cmp(f, dst_dir / f.name, shallow=False)
            )
        ]
        if not changed:
            skipped += 1
            continue
        for f in changed:
            shutil.copy2(f, dst_dir / f.name)
        verb = "updated" if existed else "deployed"
        print(
            f"[green]✔[/green] Skill {verb}:{'' if existed else '   '}    "
            f"[cyan]{src_dir.name}[/cyan]  ({len(changed)} file(s))"
        )
        if existed:
            updated += 1
        else:
            copied += 1

    if skipped and not copied and not updated and not quiet:
        print("[yellow]![/yellow] All skills already up to date in workspace/skills/  (skipped)")


if __name__ == "__main__":
    setup()
