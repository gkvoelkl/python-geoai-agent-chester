"""Fitness functions for the package cut — the one rule that carries it, as a law.

Since 2026-09-19 the library is five workspace packages under `packages/`, all
contributing to the namespace package `chester`:

    geo-tools  <-  mcp                  adapter for foreign clients
    geo-tools  <-  runtime              Chester on SelmaKit: gate hook, guards, vision
    geo-tools, runtime  <-  agent       adapter: one agent
    geo-tools, runtime  <-  team        adapter: a multi-agent (orchestrator + ressorts)
    agent      x   team                 no dependency, in either direction

chester-runtime was added the same day as the fifth package: agent and team both
run on SelmaKit and need the same gate and guards, but must not import each other.

Because the import names did not change (`chester.gate` stays `chester.gate`), the
direction is not visible at the import site. It is visible here: every module is
mapped to the package that ships it, and every import is checked against the
allowed edges. The AST decides, not a grep — a docstring that *mentions*
`chester.capabilities` is not an import.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ROOT / "packages"

GEO, RUNTIME = "chester-geo-tools", "chester-runtime"
AGENT, MCP, TEAM = "chester-agent", "chester-mcp", "chester-team"
# Which sibling packages each package may import from (itself is always allowed).
ALLOWED = {GEO: set(), RUNTIME: {GEO}, MCP: {GEO}, AGENT: {GEO, RUNTIME}, TEAM: {GEO, RUNTIME}}
# Third-party frameworks that mark a module as belonging to one adapter.
FRAMEWORKS = {
    GEO: {"selmakit", "pydantic_ai", "fastmcp", "mcp"},
    RUNTIME: {"fastmcp", "mcp"},
    MCP: {"selmakit", "pydantic_ai"},
    TEAM: set(),
    AGENT: {"fastmcp", "mcp"},
}


def _module_name(pkg_dir: Path, path: Path) -> str:
    rel = path.relative_to(pkg_dir).with_suffix("")
    parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
    return ".".join(parts)


def _owner_map() -> dict[str, str]:
    """``chester.x.y`` → the package that ships it."""
    owners = {}
    for pkg in ALLOWED:
        pkg_dir = PACKAGES / pkg
        for path in (pkg_dir / "chester").rglob("*.py"):
            if "resources" in path.parts:
                continue  # harness scripts shipped as data, not importable modules
            owners[_module_name(pkg_dir, path)] = pkg
    return owners


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(errors="replace"))):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            # `from chester import gate` imports the module `chester.gate`
            names |= {f"{node.module}.{a.name}" for a in node.names}
    return names


def _owner_of(name: str, owners: dict[str, str]) -> str | None:
    while name:
        if name in owners:
            return owners[name]
        name = name.rpartition(".")[0]
    return None


def test_all_five_packages_exist():
    missing = [p for p in ALLOWED if not (PACKAGES / p / "pyproject.toml").is_file()]
    assert not missing, f"packages missing: {missing}"


@pytest.mark.parametrize("pkg", sorted(ALLOWED))
def test_imports_follow_the_package_direction(pkg):
    owners = _owner_map()
    offenders = {}
    for path in sorted((PACKAGES / pkg / "chester").rglob("*.py")):
        if "resources" in path.parts:
            continue
        bad = set()
        for name in _imports(path):
            if name.split(".")[0] in FRAMEWORKS[pkg]:
                bad.add(name)
                continue
            if not name.startswith("chester."):
                continue
            owner = _owner_of(name, owners)
            if owner and owner != pkg and owner not in ALLOWED[pkg]:
                bad.add(f"{name} ({owner})")
        if bad:
            offenders[str(path.relative_to(PACKAGES))] = sorted(bad)
    assert not offenders, (
        f"{pkg} imports against the direction: {offenders}. Allowed: "
        f"{sorted(ALLOWED[pkg]) or 'nothing'} plus itself. geo-tools never imports "
        "upward; agent and team never import each other."
    )


def test_no_package_shadows_the_namespace():
    """A `chester/__init__.py` in any one package turns the namespace into a regular
    package — and the other portions silently vanish from `chester.__path__`."""
    shadows = [str(p.relative_to(ROOT)) for p in PACKAGES.glob("*/chester/__init__.py")]
    assert not shadows, f"namespace shadowed by {shadows}"


def test_declared_dependencies_follow_the_same_direction():
    """The pyproject edges must say what the imports do — or `uv` installs a lie."""
    siblings = set(ALLOWED)
    wrong = {}
    for pkg, allowed in ALLOWED.items():
        project = tomllib.loads((PACKAGES / pkg / "pyproject.toml").read_text())["project"]
        deps = {d.split("[")[0].split(">")[0].split("=")[0].strip()
                for d in project.get("dependencies", [])}
        extra = (deps & siblings) - allowed
        if extra:
            wrong[pkg] = sorted(extra)
    assert not wrong, f"undeclared-direction dependencies: {wrong}"


def test_one_version_across_the_repository():
    """One repo, one release cadence: the root and all five packages carry one number.

    Replaces the old check of `chester.__version__`, which a namespace package cannot
    carry. The original failure it guarded against stays the same: `pyproject`
    counted on to 0.1.2 while the package still reported 0.1.0.
    """
    root = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    versions = {
        p: tomllib.loads((PACKAGES / p / "pyproject.toml").read_text())["project"]["version"]
        for p in ALLOWED
    }
    off = {p: v for p, v in versions.items() if v != root}
    assert not off, f"versions differ from the root ({root}): {off} — bump all six together"


def test_the_namespace_is_whole_at_runtime():
    import chester

    portions = {Path(p).parent.name for p in chester.__path__}
    assert portions == set(ALLOWED), f"chester.__path__ covers {sorted(portions)}"


def test_no_stray_chester_directory_at_the_root():
    """The old home of the library must stay gone.

    pytest puts the repo root on `sys.path` (`pythonpath = ["."]`). A `chester/`
    directory there — say, a new module created where the code used to live — would
    become a fifth portion of the namespace: importable, untested by the package
    direction, shipped by no package. Nothing would fail; that is the problem.
    """
    assert not (ROOT / "chester").exists(), "a top-level chester/ is back — move it into a package"
