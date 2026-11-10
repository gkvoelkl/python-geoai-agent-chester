"""Test-Level 1: every module has a code-map entry, and mutate.sh points at real files.

Both guard the same failure: a move or a new file that leaves a reference behind
without anything turning red.

* The code map is the agreed entry point into the code (`CLAUDE.md`, "Where to read
  what"). On 2026-09-19 `vectorops` was missing from it; on 2026-10-14 four more were
  (`statsources`, `runtime/geopython`, `runtime/mapinspect`, `planpanel.py`), the
  oldest five weeks after it was written. Fixing the entries twice is a pattern, so
  this test makes it a law.
* `mutate.sh` mutated `chester/<module>.py` for two months after the package cut moved
  those files under `packages/`. mutmut aborted, the script exited 0 and printed its
  interpretation text as if a score had been measured. The script now fails loudly;
  this test catches the path drift before anyone runs it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE_MAP = ROOT / "doc" / "code-map.md"
MUTATE = ROOT / "mutate.sh"


def _package_modules() -> list[Path]:
    return sorted(
        p
        for p in (ROOT / "packages").glob("*/chester/**/*.py")
        if p.name != "__init__.py" and "__pycache__" not in p.parts
    )


def _documented(path: Path, text: str) -> bool:
    """A module counts as documented under any of the spellings the code map uses."""
    rel = "/".join(path.relative_to(ROOT / "packages").parts[1:])
    return (
        rel in text  # chester/runtime/geopython.py
        or f"`{path.stem}`" in text  # `statsources`
        or f"{path.stem}.py" in text  # statsources.py, planpanel.py
    )


def test_every_package_module_appears_in_the_code_map():
    text = CODE_MAP.read_text(encoding="utf-8")
    missing = [
        p.relative_to(ROOT).as_posix() for p in _package_modules() if not _documented(p, text)
    ]
    assert not missing, f"modules without an entry in doc/code-map.md: {missing}"


def test_every_root_script_appears_in_the_code_map():
    text = CODE_MAP.read_text(encoding="utf-8")
    missing = sorted(p.name for p in ROOT.glob("*.py") if p.name not in text)
    assert not missing, f"root scripts without an entry in doc/code-map.md: {missing}"


def test_mutate_sh_default_modules_exist():
    script = MUTATE.read_text(encoding="utf-8")
    core = re.search(r"^CORE=(\S+)$", script, re.M)
    defaults = re.search(r"modules=\(([^)]*)\); fi", script)
    assert core and defaults, "mutate.sh: CORE= or the default module list is gone — adapt"
    missing = [
        m for m in defaults.group(1).split() if not (ROOT / core.group(1) / f"{m}.py").is_file()
    ]
    assert not missing, f"mutate.sh mutates modules missing under {core.group(1)}: {missing}"


def test_mutate_sh_named_paths_exist():
    """The bank's checkers live outside CORE; `path_for` names them — and must stay true."""
    script = MUTATE.read_text(encoding="utf-8")
    paths = re.findall(r'echo "(packages/[^"$]+)\$1\.py"', script)
    names = {"chester-agent": ["benchchecks", "failclass"], "chester-code": ["programcheck"]}
    assert paths, "mutate.sh: path_for is gone — adapt"
    missing = [f"{p}{m}.py" for p in paths for pkg, ms in names.items() if pkg in p for m in ms
               if not (ROOT / f"{p}{m}.py").is_file()]
    assert not missing, f"mutate.sh names modules that do not exist: {missing}"
