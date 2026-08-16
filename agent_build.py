"""Chester — capability wiring (single source of truth).

On the new SelmaKit, the runtime (model, stores, memory, cron, channels) is
built by ``Gateway.from_config``; Chester only contributes its *capabilities*.
This module is that contribution: ``geo_capabilities(workspace_dir)`` is the geo
domain set passed via ``extra_capabilities`` to ``Gateway.from_config`` in both
``gateway.py`` and ``ask.py`` — so the agent is wired in exactly one place.

Identity/persona is *not* set here — it lives in the workspace files
(``SOUL.md`` / ``IDENTITY.md``, created by ``setup.py``) and is injected by
SelmaKit's WorkspacePromptCapability, so there is no hard-coded system prompt.
"""

from __future__ import annotations

import asyncio
import json
import random
import shutil
from pathlib import Path

from selmakit.commands import RunPrompt

from chester import geoconfig
from chester.capabilities import (
    DataDiscoveryCapability,
    GeoBoundariesCapability,
    GeoCityModelCapability,
    GeoConnectorsCapability,
    GeoInventoryCapability,
    GeoLiveCapability,
    GeoLod2Capability,
    GeoPyCapability,
    GeoStatisticsCapability,
    GeoTransitCapability,
    GeoValidationCapability,
    MapOutputCapability,
    PerceptionCapability,
    QgisToolboxCapability,
    VectorCapability,
)
from chester.geocache import DEFAULT_TTL_DAYS, GeoCache, start_periodic_sync

# Defined in chester.geoconfig so the LLM-free CLIs can read the same config
# without importing SelmaKit; re-exported here, where callers expect them.
STATE_DIR = geoconfig.STATE_DIR
CONFIG_NAME = geoconfig.CONFIG_NAME
WORKSPACE_DIR = f"{STATE_DIR}/workspace"


def _load_geodata() -> dict:
    """The ``geodata`` block from ``.chester/chester.json`` (best-effort).

    Thin alias for :func:`chester.geoconfig.load_geodata`, which the LLM-free
    CLIs share so retention settings can't drift between agent and ``data.py``.
    """
    return geoconfig.load_geodata(STATE_DIR, CONFIG_NAME)


def _config_base_url() -> str:
    """The ``model.base_url`` from the config (the Ollama OpenAI endpoint)."""
    try:
        cfg = json.loads((Path(STATE_DIR) / CONFIG_NAME).read_text())
        return (cfg.get("model") or {}).get("base_url") or ""
    except (OSError, ValueError):
        return ""


def _config_vision_model() -> str:
    """The ``model.vision_model`` fallback from the config (may be empty).

    We *assume* the main model can see images; if it reports it cannot, the agent
    routes the snapshot to this dedicated vision model instead (see MapOutput's
    ``inspect_map``). Empty → no fallback available.
    """
    try:
        cfg = json.loads((Path(STATE_DIR) / CONFIG_NAME).read_text())
        return (cfg.get("model") or {}).get("vision_model") or ""
    except (OSError, ValueError):
        return ""


def geo_capabilities(workspace_dir: str = WORKSPACE_DIR) -> list:
    """Chester's geo domain capabilities, all bound to the workspace dir.

    Appended to SelmaKit's ``default_capabilities`` via
    ``Gateway.from_config(extra_capabilities=...)``. Every tool resolves its path
    arguments into the GeoCache working dir ``<workspace_dir>/geocache/`` (see
    ``chester/workspace.py``), so multi-step workflows stay consistent and outputs
    land in the inventoried, self-expiring cache (``GeoInventoryCapability``).

    The ``geodata`` config block (read-only data roots + an optional PostGIS DSN)
    is read from ``.chester/chester.json`` and threaded into the inventory (so
    in-place roots are catalogued as ``source: user``) and the container
    connectors. Unconfigured → those features are inert.
    """
    gd = _load_geodata()
    roots = gd["roots"]
    return [
        QgisToolboxCapability(workspace=workspace_dir),
        DataDiscoveryCapability(workspace=workspace_dir, stac_catalogs=gd["stac_catalogs"]),
        PerceptionCapability(workspace=workspace_dir),
        VectorCapability(workspace=workspace_dir),
        GeoValidationCapability(workspace=workspace_dir),
        MapOutputCapability(
            workspace=workspace_dir,
            vision_model=_config_vision_model(),
            base_url=_config_base_url(),
        ),
        GeoInventoryCapability(
            workspace=workspace_dir,
            roots=roots,
            default_ttl_days=gd["ttl_days"] or DEFAULT_TTL_DAYS,
            ttl_by_source=gd["ttl_by_source"],
        ),
        GeoConnectorsCapability(workspace=workspace_dir, roots=roots, postgis=gd["postgis"]),
        GeoLod2Capability(workspace=workspace_dir),
        GeoBoundariesCapability(workspace=workspace_dir),
        GeoCityModelCapability(workspace=workspace_dir),
        GeoStatisticsCapability(workspace=workspace_dir, statistics=gd["statistics"]),
        GeoTransitCapability(workspace=workspace_dir),
        GeoLiveCapability(workspace=workspace_dir),
        GeoPyCapability(workspace=workspace_dir),
    ]


def _capability_tools(capability) -> dict:
    """{tool_name: callable} for a capability's FunctionToolset (one source of truth)."""
    toolset = capability.get_toolset()
    out = {}
    for name, tool in toolset.tools.items():
        fn = getattr(tool, "function", None) or getattr(tool, "func", None) or tool
        out[name] = fn
    return out


def _fmt_geocache(rows: list) -> str:
    if not rows:
        return "GeoCache is empty."
    lines = ["**GeoCache**", "", "| dataset | kind | CRS | size | expires |",
             "|---|---|---|---|---|"]
    for r in rows:
        if r["kind"] == "raster":
            size = f"{r['size'][0]}×{r['size'][1]}px"
            kind = "raster"
        else:
            size = f"{r['features']} feat"
            kind = f"vector:{r['geometry_type']}"
        # Mirror geocache.md's marker so a pinned (manually kept) dataset is
        # visibly different from one on the configured retention.
        expires = f"{r['expires']}*" if r.get("ttl_pinned") else r["expires"]
        lines.append(f"| {r['dataset']} | {kind} | {r['crs'] or '-'} | {size} | {expires} |")
    if any(r.get("ttl_pinned") for r in rows):
        lines += ["", "_`*` = pinned retention (`geocache_note`), not the configured default._"]
    return "\n".join(lines)


PROMPTS_PATH = Path(__file__).resolve().parent / "agent-test-prompts.jsonl"


def _load_test_prompts() -> list[dict]:
    """Read the JSONL benchmark test bank (one test object per line, best-effort)."""
    try:
        lines = PROMPTS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(s) for s in lines if s.strip()]


def _fmt_testprompts(tests: list[dict]) -> str:
    lines = [
        "**Test prompts** — run one in-chat with `/testprompt <id>` "
        "(`--random` a random test · `--fresh` clear the cache first):",
        "",
    ]
    for t in tests:
        prompt = t.get("prompt_de") or ""
        lines.append(f"- `{t['id']}` — {t.get('level', '-')} · {t.get('category', '-')}")
        lines.append(f"  {prompt}")
    lines.append("")
    lines.append(f"{len(tests)} test(s).")
    return "\n".join(lines)


def register_validation_gate(
    agent, workspace_dir: str = WORKSPACE_DIR, state_dir: str = STATE_DIR
) -> None:
    """Register Chester's enforcing validation gate as an output validator.

    A post-run ``output_validator`` (SelmaKit passthrough) over datasets the run
    produced *and* the answer mentions: the level-1 structural checks raise
    ``ModelRetry`` once on a real defect, and at level ≥2 the visual check renders the
    result and asks the configured ``model.vision_model`` for an advisory second
    opinion (see ``chester/gate.py`` and ``doc/validation-concept.md`` §4.1).
    Registered from **both** entrypoints (``gateway.py`` and ``ask.py``) so the gate
    is a true loop phase, not a web-only feature. The per-session strictness is set
    with ``/valid_level`` (registered in ``register_geo_commands``); unset defaults
    to level 1.
    """
    from chester.gate import make_validation_gate

    gate = make_validation_gate(
        sessions_dir=str(Path(state_dir) / "sessions"),
        workspace=workspace_dir,
        vision_model=_config_vision_model(),
        base_url=_config_base_url(),
    )
    agent.output_validator(gate)


def start_geocache_sync(workspace_dir: str = WORKSPACE_DIR):
    """Start the periodic GeoCache sync if ``geodata.sync_interval_hours`` is set.

    The inventory is reconciled (and expired datasets deleted) at every startup
    and before each ``geocache_list``, which covers short CLI runs. A gateway,
    though, can stay up for days — this closes that gap by re-syncing on an
    interval. Off by default (interval ``0``): a single-user local agent restarts
    often enough that it is opt-in, not an imposed background job.

    Returns the stop :class:`threading.Event`, or ``None`` when disabled. Called
    from ``gateway.py`` only — ``ask.py`` is one-shot, where the startup sync is
    already the whole story.
    """
    gd = _load_geodata()
    hours = gd["sync_interval_hours"]
    if hours <= 0:
        return None
    cache = GeoCache.from_config(workspace_dir, gd)
    return start_periodic_sync(cache, hours)


def register_geo_commands(  # noqa: C901, PLR0915
        # Ausnahme: sieben Slash-Befehle als verschachtelte async defs. Komplexitaet und
        # Anweisungszahl messen hier ihre *Anzahl*, nicht verworrenen Code — dieselbe
        # Lage wie in get_toolset, das dafuer eine per-file-ignores-Regel hat.
        agent, workspace_dir: str = WORKSPACE_DIR) -> None:
    """Register Chester's GeoCache/connector slash commands on the agent.

    Channel-intercepted (SelmaKit runs them before the LLM, no agent turn). Each
    is a thin formatter over the **same** ``GeoCache`` / connector callables the
    tools use, so a command and its tool can't drift. Registered directly on the
    agent instance (SelmaKit's documented pattern) — call this from ``gateway.py``
    after ``Gateway.from_config(...)``. Delete/prune live here as deliberate user
    commands, never as autonomous agent tools.
    """
    gd = _load_geodata()
    cache = GeoCache(workspace=workspace_dir, roots=gd["roots"])
    conn = _capability_tools(
        GeoConnectorsCapability(workspace=workspace_dir, roots=gd["roots"], postgis=gd["postgis"])
    )

    @agent.command("/geocache")
    async def _geocache(ctx) -> str:
        """Show the GeoCache inventory; `prune [--dry-run]`, `rm <dataset>`,
        or `rm all [--dry-run]`."""
        arg = ctx.args.strip()
        head = arg.split(None, 1)[:1]
        if head == ["prune"]:
            dry = "--dry-run" in arg
            r = cache.prune(dry_run=dry)
            if not r["expired"]:
                return "GeoCache prune: nothing is expired."
            verb = "Would delete" if dry else "Deleted"
            body = "\n".join(f"- {k}" for k in r["expired"])
            mode = 'dry run' if dry else 'done'
            return (f"GeoCache prune ({mode}) — {verb} "
                    f"{len(r['expired'])}:\n{body}")
        if head == ["rm"]:
            target = arg[2:].strip()
            if not target:
                return "Usage: `/geocache rm <dataset>` (or `rm all [--dry-run]`)"
            if target.split()[0] == "all":
                dry = "--dry-run" in target
                r = cache.remove_all(dry_run=dry)
                if not r["removed"]:
                    return "GeoCache is already empty (nothing to remove)."
                verb = "Would delete" if dry else "Deleted"
                body = "\n".join(f"- {k}" for k in r["removed"])
                kept = (f"\n\n_(kept {len(r['kept'])} `source: user` dataset(s) — "
                        "referenced data roots)_" if r["kept"] else "")
                return (f"GeoCache rm all ({'dry run' if dry else 'done'}) — "
                        f"{verb} {len(r['removed'])}:\n{body}{kept}")
            r = cache.remove(target)
            return f"Removed: {', '.join(r['removed'])}" if r["ok"] else f"⚠️ {r['error']}"
        return _fmt_geocache(cache.list(filter=arg or None))

    @agent.command("/geoconnector")
    async def _geoconnector(ctx) -> str:
        """List the configured GeoConnectors (query + container)."""
        r = conn["geoconnectors_list"]()
        query = ", ".join(c["name"] for c in r["query_connectors"])
        lines = ["**GeoConnectors**", "", f"_query:_ {query}", "", "_containers:_"]
        if not r["container_connectors"]:
            lines.append("- none configured (set `geodata.roots` / `geodata.postgis`)")
        for c in r["container_connectors"]:
            extra = f" (schema {c['schema']})" if c.get("schema") else ""
            lines.append(f"- `{c['name']}` — {c['kind']}{extra}")
        return "\n".join(lines)

    @agent.command("/geodataset")
    async def _geodataset(ctx) -> str:
        """List datasets in a container: `/geodataset <connector>`."""
        connector = ctx.args.strip()
        if not connector:
            names = [c["name"] for c in conn["geoconnectors_list"]()["container_connectors"]]
            avail = ", ".join(f"`{n}`" for n in names) or "none configured"
            return f"Usage: `/geodataset <connector>`\nContainers: {avail}"
        r = conn["geodatasets_list"](connector=connector)
        if not r["ok"]:
            return f"⚠️ {r['error']}"
        if not r["datasets"]:
            return f"No datasets in `{connector}`."
        lines = [f"**{connector}** — {r['count']} dataset(s):", ""]
        for d in r["datasets"]:
            lines.append(f"- `{d['dataset']}` — {d.get('geometry_type', '?')}, "
                         f"{d.get('crs', '?')}, {d.get('features', '?')} feat")
        return "\n".join(lines)

    @agent.command("/testprompt")
    async def _testprompt(ctx):
        """Run a benchmark test prompt in-chat: `/testprompt <id>`
        (`--random` a random test, `--fresh` clear the GeoCache first). No id lists all."""
        tests = _load_test_prompts()
        if not tests:
            return "No test prompts found."
        words = ctx.args.split()
        flags = {w.lstrip("-").lower() for w in words}
        rand = "random" in flags
        fresh = "fresh" in flags
        # the test id is the lone non-flag word (flags are `--x` or bare keywords)
        test_id = " ".join(
            w for w in words
            if not w.startswith("-") and w.lower() not in {"random", "fresh"}
        ).strip()

        if rand:
            test = random.choice(tests)
        elif not test_id:
            return _fmt_testprompts(tests)
        else:
            test = next((t for t in tests if t["id"] == test_id), None)
            if test is None:
                ids = ", ".join(f"`{t['id']}`" for t in tests)
                return f"Unknown test id: `{test_id}`.\nAvailable: {ids}"

        prompt = test.get("prompt_de")
        if not prompt:
            return f"Test `{test['id']}` has no prompt text."

        if fresh:
            # Clear the GeoCache before the run so it re-fetches from scratch.
            # Best-effort: a wipe failure must not block the prompt.
            geocache_dir = GeoCache(workspace=workspace_dir).geocache_dir
            try:
                if geocache_dir.exists():
                    shutil.rmtree(geocache_dir)
            except OSError:
                pass

        # RunPrompt → SelmaKit rewrites-and-runs it as a real streamed agent turn.
        # (The rewritten prompt is echoed in chat, so a --random pick is visible.)
        return RunPrompt(text=prompt)

    @agent.command("/eval")
    async def _eval(ctx) -> str:
        """Show the benchmark eval history: pass-rate + mean tool-coverage per model
        and the latest verdict per test. `/eval <filter>` narrows by test id or model."""
        from chester import evalhistory

        history_path = Path(STATE_DIR) / "evals" / "history.jsonl"
        records = evalhistory.load_history(history_path)
        return evalhistory.format_report(records, filter=ctx.args.strip() or None)

    @agent.command("/valid_level")
    async def _valid_level(ctx) -> str:
        """Show or set the result-validation strictness for this session: `/valid_level <0-3>`.

        Cumulative — level n runs the checks of 1…n. 0 off · 1 structural (default) ·
        2 +visual · 3 +redundancy. See `doc/validation-concept.md` §4.1."""
        from chester.gate import (
            DEFAULT_LEVEL,
            MAX_LEVEL,
            MIN_LEVEL,
            VALID_LEVEL_KEY,
            clamp_level,
            level_description,
            levels_overview,
        )

        arg = ctx.args.strip()
        if not arg:
            cur = clamp_level(ctx.session.get(VALID_LEVEL_KEY, DEFAULT_LEVEL))
            return (
                f"**Validation level `{cur}`** — {level_description(cur)}\n\n"
                f"{levels_overview()}\n\n_Set with `/valid_level <0-3>`._"
            )
        try:
            n = int(arg)
        except ValueError:
            return f"Usage: `/valid_level <{MIN_LEVEL}-{MAX_LEVEL}>` (got `{arg}`)."
        if not MIN_LEVEL <= n <= MAX_LEVEL:
            return f"Level must be {MIN_LEVEL}–{MAX_LEVEL} (got `{n}`)."
        ctx.session.set(VALID_LEVEL_KEY, n)
        return f"Validation level set to `{n}` — {level_description(n)}"

    @agent.command("/qgis")
    async def _qgis(ctx) -> str:
        """Show the last rendered map's layers in live QGIS Desktop (reuses a running one)."""
        pointer = Path(workspace_dir) / "geocache" / "last_map.json"
        if not pointer.exists():
            return "No map rendered yet — create a map first, then `/qgis`."
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "⚠️ Could not read the last-map pointer (`last_map.json`)."
        recorded = data.get("layers", [])
        layers = [p for p in recorded if Path(p).exists()]
        if not layers:
            return ("The last map's source layers are no longer on disk "
                    "(pruned/expired?). Re-run the map, then `/qgis`.")
        # Go through the live bridge (same path as the qgis_show tool): reuse a
        # running QGIS if there is one, else launch a windowed QGIS.
        from chester import qgis_live_client as live

        try:
            # Convert big/mixed GeoJSON to a cached GeoPackage first (off the event
            # loop — the first conversion of a large layer can take ~30 s).
            loadable = [await asyncio.to_thread(live.to_loadable, p) for p in layers]
            state = live.ensure_running()
            live._call("add_layers", paths=loadable, timeout=120.0)
            live._call("zoom_full")
        except live.QgisBridgeError as exc:
            return f"⚠️ {exc}"
        verb = "Launched QGIS with" if state == "launched" else "Added to running QGIS"
        status = (f"{verb} {len(layers)} layer(s) from the last map:\n"
                  + "\n".join(f"- `{Path(p).name}`" for p in layers))
        missing = len(recorded) - len(layers)
        if missing:
            status += f"\n\n_(skipped {missing} layer(s) no longer on disk)_"
        return status
