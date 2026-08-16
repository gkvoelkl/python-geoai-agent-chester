"""Chester — GeoCache inventory viewer (no LLM, no SelmaKit).

Prints the local GeoCache: every cached dataset with its kind, CRS, extent, age
and expiry. Mirrors what the agent sees via ``geocache_list`` — the same
:class:`chester.geocache.GeoCache` code path *and* the same ``geodata`` config
(data roots, retention), so the CLI and the tool can't drift. Reading always
reconciles with disk first (and prunes expired datasets).

Usage:
    uv run data.py                  # sync + print the inventory
    uv run data.py --filter ahr     # only datasets matching a substring
    uv run data.py --prune          # force a sync and report what expired
"""

from __future__ import annotations

import argparse
from datetime import date

from chester.geocache import GeoCache
from chester.workspace import DEFAULT_WORKSPACE


def _fmt_age(last_used: str, today: str) -> str:
    days = (date.fromisoformat(today) - date.fromisoformat(last_used)).days
    if days <= 0:
        return "today"
    return f"{days}d ago"


def _print_table(rows: list[dict], today: str) -> None:
    if not rows:
        print("GeoCache is empty.")
        return
    headers = ["dataset", "kind", "crs", "size", "used", "expires"]
    table = []
    for r in rows:
        size = (f"{r['size'][0]}x{r['size'][1]}px" if r["kind"] == "raster"
                else f"{r['features']} feat")
        table.append([
            r["dataset"],
            (f"vector:{r['geometry_type']}" if r["kind"] == "vector" else "raster"),
            r["crs"] or "-",
            size,
            _fmt_age(r["last_used"], today),
            r["expires"],
        ])
    widths = [max(len(h), *(len(row[i]) for row in table)) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in table:
        print("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers))))
    notes = [(r["dataset"], r["note"]) for r in rows if r["note"]]
    if notes:
        print("\nNotes:")
        for name, note in notes:
            print(f"  {name}: {note}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect Chester's GeoCache inventory.")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="workspace dir to scan")
    parser.add_argument("--filter", help="only datasets whose name/note/CRS/kind matches")
    parser.add_argument("--prune", action="store_true", help="sync and report expired datasets")
    args = parser.parse_args()

    today = date.today().isoformat()
    # From the same `geodata` config the agent uses — data roots and retention
    # included — so a prune here can't evict what the agent means to keep.
    cache = GeoCache.from_config(args.workspace)

    if args.prune:
        summary = cache.sync(today=today)
        print(
            f"Synced {summary['total']} dataset(s): "
            f"+{len(summary['added'])} added, "
            f"{len(summary['expired'])} expired, {len(summary['dropped'])} dropped."
        )
        if summary["expired"]:
            print("Expired (deleted):")
            for key in summary["expired"]:
                print(f"  {key}")
        return

    rows = cache.list(filter=args.filter, today=today)
    print(f"GeoCache · {cache.inventory_path}\n")
    _print_table(rows, today)


if __name__ == "__main__":
    main()
