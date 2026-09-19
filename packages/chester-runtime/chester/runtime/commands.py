"""The slash commands every Chester agent offers — cache, connectors, strictness.

Channel-intercepted (SelmaKit runs them before the LLM, no agent turn). Each is a
thin formatter over the **same** ``GeoCache`` / connector callables the tools use, so
a command and its tool cannot drift. Delete/prune live here as deliberate user
commands, never as autonomous agent tools. Moved out of the root `agent_build.py`
(2026-09-19, KP.5 T0) so chester-team's orchestrator gets them too; the connector
callables now come from the wrapper layer directly instead of via the capability.
The agent-specific commands (`/testprompt`, `/eval`, `/qgis`) stay with its builder.
"""

from __future__ import annotations

from chester import connectorstools
from chester.geocache import GeoCache
from chester.runtime.config import WORKSPACE_DIR, load_geodata


def register_runtime_commands(  # noqa: C901, PLR0915
    # Exception: four slash commands as nested async defs — the count measures the
    # commands, not tangled code (same situation as get_toolset).
    agent,
    workspace_dir: str = WORKSPACE_DIR,
) -> None:
    """Register `/geocache`, `/geoconnector`, `/geodataset` and `/valid_level`."""
    gd = load_geodata()
    cache = GeoCache(workspace=workspace_dir, roots=gd["roots"])
    conn = {
        f.__name__: f
        for f in connectorstools.build_tools(workspace_dir, gd["roots"], gd["postgis"])
    }

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
            mode = "dry run" if dry else "done"
            return f"GeoCache prune ({mode}) — {verb} {len(r['expired'])}:\n{body}"
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
                kept = (
                    f"\n\n_(kept {len(r['kept'])} `source: user` dataset(s) — "
                    "referenced data roots)_"
                    if r["kept"]
                    else ""
                )
                return (
                    f"GeoCache rm all ({'dry run' if dry else 'done'}) — "
                    f"{verb} {len(r['removed'])}:\n{body}{kept}"
                )
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
            lines.append(
                f"- `{d['dataset']}` — {d.get('geometry_type', '?')}, "
                f"{d.get('crs', '?')}, {d.get('features', '?')} feat"
            )
        return "\n".join(lines)

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


def _fmt_geocache(rows: list) -> str:
    if not rows:
        return "GeoCache is empty."
    lines = [
        "**GeoCache**",
        "",
        "| dataset | kind | CRS | size | expires |",
        "|---|---|---|---|---|",
    ]
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
