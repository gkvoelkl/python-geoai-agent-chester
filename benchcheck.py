"""Chester — the mechanical checks of a bank prompt, on its last run (Phase MB).

    uv run benchcheck.py buffer-schools-500m          # the last run of this prompt
    uv run benchcheck.py --list                       # prompts that carry checks
    uv run benchcheck.py <id> --workspace <dir>       # files copied elsewhere
    uv run benchcheck.py --compare                    # MB.3: mechanical vs. judge, history

Reads the session `testprompt:<id>` that `testprompt.py` left and the files in the
workspace, and runs the record's ``checks`` (`chester/benchchecks.py`): tools called,
CRS and count of the produced layers, numbers in the answer. **Run it right after the
run** — the next `--fresh` run clears the GeoCache, and with it the files to check.
Exit code 0 when every check passed, 1 otherwise, 2 when the prompt has no checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chester import benchchecks
from chester.runtime.config import STATE_DIR

BANK = Path(__file__).parent / "agent-test-prompts.jsonl"
SESSIONS_DIR = Path(STATE_DIR) / "sessions"
GEOCACHE = Path(STATE_DIR) / "workspace" / "geocache"


def load_bank() -> dict[str, dict]:
    rows = [json.loads(line) for line in BANK.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return {r["id"]: r for r in rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("test_id", nargs="?")
    parser.add_argument("--list", action="store_true", help="prompts that carry checks")
    parser.add_argument("--workspace", type=Path, default=GEOCACHE,
                        help="where the run's files are (default: the GeoCache)")
    parser.add_argument("--session", help="session key (default: testprompt:<id>)")
    parser.add_argument("--compare", action="store_true",
                        help="judged runs of prompts with checks: mechanical vs. judge")
    args = parser.parse_args(argv)

    bank = load_bank()
    if args.compare:
        return compare(bank)
    if args.list or not args.test_id:
        for tid, row in bank.items():
            if row.get("checks"):
                print(f"{tid:40} {len(row['checks'])} Prüfungen")
        return 0
    rec = bank.get(args.test_id)
    if rec is None:
        print(f"unbekannter Prompt: {args.test_id}", file=sys.stderr)
        return 2
    if not rec.get("checks"):
        print(f"{args.test_id} hat keine mechanischen Prüfungen")
        return 2
    turn = benchchecks.turn_from_session(args.session or f"testprompt:{args.test_id}",
                                         SESSIONS_DIR)
    passed, lines = benchchecks.evaluate(rec, turn, args.workspace)
    print(f"{args.test_id}: {'PASS' if passed else 'FAIL'} "
          f"({sum(line.lstrip().startswith('✓') for line in lines)}/{len(lines)})")
    print("\n".join(lines))
    return 0 if passed else 1


def compare(bank: dict[str, dict]) -> int:
    """MB.3: every judged run of a prompt with checks, re-checked from its protocol."""
    from chester.evalhistory import load_history

    rows = benchchecks.compare_with_judge(load_history(), bank)
    agree = sum(r["judge"] == r["mechanical"] for r in rows)
    conflict = sum(r["judge"] and not r["mechanical"] for r in rows)
    beyond = sum(r["mechanical"] and not r["judge"] for r in rows)
    print(f"{len(rows)} Läufe (ohne QGIS) — gleich {agree} · Konflikt (Judge PASS, mechanisch "
          f"FAIL) {conflict} · Judge sah mehr (mechanisch PASS, Judge FAIL) {beyond}\n")
    for r in rows:
        mark = "=" if r["judge"] == r["mechanical"] else "≠"
        print(f"{mark} {r['ts']} {r['test']:38} Judge {'PASS' if r['judge'] else 'FAIL'}"
              f" · mechanisch {'PASS' if r['mechanical'] else 'FAIL'}  ({r['model']})")
        for why in r["failed"] if r["judge"] != r["mechanical"] else []:
            print(f"      ✗ {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
