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

from chester.capabilities import (
    DataDiscoveryCapability,
    GeoBoundariesCapability,
    GeoCityModelCapability,
    GeoConnectorsCapability,
    GeoCoreCapability,
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
from chester.geocache import DEFAULT_TTL_DAYS, GeoCache
from chester.qgis_env import qgis_available

# The base every Chester agent shares lives in chester-runtime (2026-09-19, KP.5 T0);
# this module adds what makes *this* agent: the geo capabilities and its bench
# commands. The names below are imported so callers keep `from agent_build import …`.
from chester.runtime.commands import register_runtime_commands
from chester.runtime.config import (  # noqa: F401  # re-exported for the entry points
    CONFIG_NAME,
    STATE_DIR,
    WORKSPACE_DIR,
    config_base_url,
    config_main_model,
    config_vision_model,
    load_geodata,
)
from chester.runtime.wiring import (  # noqa: F401  # re-exported for the entry points
    base_capabilities,
    register_validation_gate,
    selmakit_capabilities,
    start_geocache_sync,
)


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
    gd = load_geodata()
    roots = gd["roots"]
    capabilities = [
        *base_capabilities(workspace_dir),
        DataDiscoveryCapability(workspace=workspace_dir, stac_catalogs=gd["stac_catalogs"]),
        PerceptionCapability(workspace=workspace_dir),
        VectorCapability(workspace=workspace_dir),
        # Raster, Terrain, Netzwerk — die Geschwister der neun
        # Vektorwerkzeuge, ebenfalls ohne QGIS (Phase KQ).
        GeoCoreCapability(workspace=workspace_dir),
        GeoValidationCapability(workspace=workspace_dir),
        MapOutputCapability(
            workspace=workspace_dir,
            vision_model=config_vision_model(),
            base_url=config_base_url(),
            main_model=config_main_model(),
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
    ]
    # QGIS ist seit dem 2026-09-06 eine **Option** (Phase KQ). Der Rechenkern liegt in
    # `geoops`/`rasterops`/`terrainops`/`networkops` und ist über `geo_python_run`
    # erreichbar; wer QGIS installiert hat, bekommt zusätzlich den Katalog aus 761
    # Algorithmen (`qgis_search`/`qgis_run`), den PyQGIS-Notausgang und die
    # Desktop-Brücke. Fehlt es, bleiben diese drei Fähigkeiten **ganz** draußen statt
    # als Werkzeuge, die beim ersten Aufruf `QgisNotFoundError` werfen — gemessen
    # 2026-09-06: der Agent baut auch ohne QGIS durch, aber `qgis_search` warf, und
    # neunzehn unbenutzbare Werkzeuge standen im Prompt.
    # Gefiltert wird auf **Fähigkeits**ebene, damit die Instruktionsabschnitte
    # mitgehen (dieselbe Begründung wie bei `_DROPPED_SELMAKIT_CAPABILITIES`).
    if qgis_available():
        capabilities += [
            QgisToolboxCapability(workspace=workspace_dir),
            GeoLiveCapability(workspace=workspace_dir),
            GeoPyCapability(workspace=workspace_dir),
        ]
    return capabilities


def _capability_tools(capability) -> dict:
    """{tool_name: callable} for a capability's FunctionToolset (one source of truth)."""
    toolset = capability.get_toolset()
    out = {}
    for name, tool in toolset.tools.items():
        fn = getattr(tool, "function", None) or getattr(tool, "func", None) or tool
        out[name] = fn
    return out


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


def register_geo_commands(  # noqa: C901, PLR0915
    # Ausnahme: sieben Slash-Befehle als verschachtelte async defs. Komplexitaet und
    # Anweisungszahl messen hier ihre *Anzahl*, nicht verworrenen Code — dieselbe
    # Lage wie in get_toolset, das dafuer eine per-file-ignores-Regel hat.
    agent,
    workspace_dir: str = WORKSPACE_DIR,
) -> None:
    """Register Chester's GeoCache/connector slash commands on the agent.

    Channel-intercepted (SelmaKit runs them before the LLM, no agent turn). Each
    is a thin formatter over the **same** ``GeoCache`` / connector callables the
    tools use, so a command and its tool can't drift. Registered directly on the
    agent instance (SelmaKit's documented pattern) — call this from ``gateway.py``
    after ``Gateway.from_config(...)``. Delete/prune live here as deliberate user
    commands, never as autonomous agent tools.
    """
    # The commands every Chester agent offers live in chester-runtime; the three
    # below run this agent's bench or its QGIS bridge and stay here.
    register_runtime_commands(agent, workspace_dir)

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
            w for w in words if not w.startswith("-") and w.lower() not in {"random", "fresh"}
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
            return (
                "The last map's source layers are no longer on disk "
                "(pruned/expired?). Re-run the map, then `/qgis`."
            )
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
        status = f"{verb} {len(layers)} layer(s) from the last map:\n" + "\n".join(
            f"- `{Path(p).name}`" for p in layers
        )
        missing = len(recorded) - len(layers)
        if missing:
            status += f"\n\n_(skipped {missing} layer(s) no longer on disk)_"
        return status
