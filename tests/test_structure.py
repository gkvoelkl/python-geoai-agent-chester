"""Fitness functions — tests that assert things about the *source*, not the behaviour.

The name is from Evolutionary Architecture (ArchUnit is the Java tool); in Python a
few lines over the AST do the job, with no extra dependency. Each test here turns one
rule that `CLAUDE.md` states as prose into something mechanical, on the principle that
carries Phase H: *prose is a request, a test is a law.*

Why the AST and not `grep`: `chester/lod2.py` mentions ``chester/capabilities/lod2.py``
in its module docstring. A textual search calls that an import-contract violation; the
AST sees no import and stays quiet.

Two of these are **ratchets** rather than limits. A hard cap would demand a rewrite of
inherited code before anything else could happen (`capabilities/discovery.py` alone is
~1700 lines), so instead the checked-in baseline says "no worse than today" and every
improvement tightens it.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).parent / "structure_baseline.json"

# `gate.py` is not a pure core at all — it is the agent loop's validation phase that
# happens to live in `chester/`. It must import `ModelRetry` to be a real
# `output_validator`, and it reaches into the capability layer for the optional visual
# check. Naming that honestly beats maintaining two ad-hoc allow-lists: the contracts
# below apply to the pure cores, and a separate test keeps this set from growing.
_AGENT_LAYER = {"gate.py"}
# LLM-free entry points: these must run without SelmaKit, or `data.py --prune` would
# need the whole agent stack just to list a cache.
_LLM_FREE = ("data.py", "chester/evalhistory.py")


def _pure_core_files() -> list[Path]:
    """`chester/*.py` — the pure cores, excluding the capability layer."""
    return sorted(
        p
        for p in (ROOT / "chester").glob("*.py")
        if p.name != "__init__.py" and p.name not in _AGENT_LAYER
    )


def _imported_modules(path: Path) -> set[str]:
    """Every module name imported by a file, from the AST — comments do not count."""
    tree = ast.parse(path.read_text(errors="replace"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# ── 1-3: import contracts ────────────────────────────────────────────────────


def test_pure_cores_do_not_import_selmakit_or_pydantic_ai():
    offenders = {}
    for path in _pure_core_files():
        bad = {m for m in _imported_modules(path) if m.split(".")[0] in {"selmakit", "pydantic_ai"}}
        if bad:
            offenders[path.name] = sorted(bad)
    assert not offenders, (
        f"reine Kernmodule mit Framework-Abhängigkeit: {offenders}. "
        "Sie müssen ohne SelmaKit laufen (data.py teilt sie sich mit dem Agenten)."
    )


def test_the_agent_layer_inside_chester_stays_a_single_module():
    """The exception must not spread — and must still be an exception.

    Two ways this goes wrong: another module starts importing the framework (the set
    should have caught it), or `gate.py` stops needing it (then the exemption is stale
    and should be removed rather than quietly widening the contract).
    """
    coupled = {
        p.name
        for p in (ROOT / "chester").glob("*.py")
        if p.name != "__init__.py"
        and any(
            m.split(".")[0] in {"selmakit", "pydantic_ai"} or "capabilities" in m
            for m in _imported_modules(p)
        )
    }
    assert coupled == _AGENT_LAYER, (
        f"Agentenschicht in chester/ ist {sorted(coupled)}, erwartet {sorted(_AGENT_LAYER)}. "
        "Wächst sie, ist ein Kernmodul gekoppelt worden; schrumpft sie, gehört die "
        "Ausnahme gestrichen."
    )


def test_pure_cores_do_not_import_the_capability_layer():
    offenders = {}
    for path in _pure_core_files():
        bad = {m for m in _imported_modules(path) if "capabilities" in m}
        if bad:
            offenders[path.name] = sorted(bad)
    assert not offenders, (
        f"Kernmodul importiert die Werkzeugschicht: {offenders}. "
        "Die Richtung ist capabilities → core, nie umgekehrt."
    )


@pytest.mark.parametrize("rel", _LLM_FREE)
def test_llm_free_entrypoints_stay_llm_free(rel):
    imported = _imported_modules(ROOT / rel)
    bad = {m for m in imported if m.split(".")[0] in {"agent_build", "selmakit", "pydantic_ai"}}
    assert not bad, f"{rel} zieht {sorted(bad)} herein — es muss ohne Agentenschicht laufen."


# ── 4: the capability contract ───────────────────────────────────────────────


def _capability_classes():
    for path in sorted((ROOT / "chester" / "capabilities").glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Capability"):
                yield path.name, node


def test_every_capability_implements_the_contract():
    problems = []
    for filename, node in _capability_classes():
        bases = {ast.unparse(b).split("[")[0] for b in node.bases}
        if "AbstractCapability" not in bases:
            problems.append(f"{filename}:{node.name} erbt nicht von AbstractCapability")
        methods = {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
        if "get_instructions" not in methods:
            problems.append(f"{filename}:{node.name} hat kein get_instructions")
    assert not problems, "; ".join(problems)


def test_capability_count_matches_the_agent_factory():
    # A capability that exists but is never registered is invisible to the agent.
    declared = {node.name for _f, node in _capability_classes()}
    factory = (ROOT / "agent_build.py").read_text(errors="replace")
    missing = sorted(name for name in declared if name not in factory)
    assert not missing, f"Capability nicht in geo_capabilities() verdrahtet: {missing}"


# ── 5-6: the ratchets ────────────────────────────────────────────────────────


def _load_baseline() -> dict:
    return json.loads(BASELINE.read_text()) if BASELINE.exists() else {}


def _line_counts() -> dict[str, int]:
    out = {}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        # `mutants/` und `.mutmut-cache` sind Werkzeug-Artefakte: Ein Mutationslauf
        # legt eine Kopie des Baums an und verfaelschte damit die Baseline
        # (93 -> 137 Dateien), bis das hier stand.
        # `harenessa` und `postgis_test_db` sind unveroeffentlicht (siehe
        # .gitignore): ihre Dateien duerfen in einer eingecheckten Baseline nicht
        # auftauchen, sonst beschreibt der veroeffentlichte Stand einen Baum, den
        # ein Klon nicht hat.
        if rel.startswith(
            (".venv", "cache", ".chester", "harenessa", "postgis_test_db",
             "build", "mutants", ".mutmut-cache")
        ):
            continue
        out[rel] = len(path.read_text(errors="replace").splitlines())
    return out


def test_no_file_grows_beyond_its_baseline():
    """Ratchet, not cap: today's size is the ceiling, and it only ever moves down."""
    baseline = _load_baseline().get("file_lines", {})
    if not baseline:
        pytest.skip("keine Baseline eingecheckt")
    grown = {
        rel: (n, baseline[rel])
        for rel, n in _line_counts().items()
        if rel in baseline and n > baseline[rel]
    }
    assert not grown, (
        "Dateien über ihrem Stand gewachsen (jetzt, Baseline): "
        + str(grown)
        + ". Aufteilen — oder, wenn das Wachstum begründet ist, die Baseline mit "
        "`uv run python tests/test_structure.py --update-baseline` neu setzen; "
        "der neue Wert steht dann im Diff."
    )


def test_new_files_stay_under_the_hard_limit():
    """A cap *is* possible for files with no history — 400 lines, generously."""
    baseline = _load_baseline().get("file_lines", {})
    oversized = {rel: n for rel, n in _line_counts().items() if rel not in baseline and n > 400}
    assert not oversized, (
        f"neue Datei über 400 Zeilen: {oversized}. Für Neues gilt die harte Grenze — "
        "die Ratsche schützt nur Altbestand."
    )


def _ruff_findings_per_file() -> dict[str, int]:
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format=concise", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^([\w/.-]+\.py):\d+:", line)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def test_lint_findings_do_not_grow():
    """Ruff **per file**: a count may fall, never rise.

    Per file, not one total — the same reason as the type ratchet. A single number
    lets a fix in one file pay for a new finding in another: the total stays put while
    the code gets worse where it was clean. Observed on 2026-08-13, when a fresh
    `I001` in `harenessa/cli.py` slipped past a total that happened to stay at 61 —
    the `ruff-harenessa` sensor caught it, this ratchet did not.
    """
    baseline = _load_baseline().get("ruff_files")
    if baseline is None:
        pytest.skip("keine Lint-Baseline eingecheckt")
    counts = _ruff_findings_per_file()
    grown = {f: (n, baseline.get(f, 0)) for f, n in counts.items() if n > baseline.get(f, 0)}
    assert not grown, (
        f"mehr Lint-Befunde als in der Baseline (jetzt, vorher): {grown}. Beheben — "
        "oder die Warnung mit `# noqa: <regel>  # <grund>` unterdrücken; der Grund "
        "gehört in den Code, damit die Ausnahme im Diff sichtbar bleibt."
    )


def _unpublished_or_skip(path: Path):
    """Skip a fitness function whose subject is not part of the publication.

    `CLAUDE.md`, `.claude/` and `harenessa/` are gitignored on purpose: they are the
    harness around *this* working copy, not the product. But the tests that police
    them live in this published file, so on a fresh clone they would fail on a missing
    file — and `./check.sh`, which the README offers a newcomer as the way in, would be
    red for a reason that is not a defect. Absent means "not published here", so skip.
    Found 2026-08-16 while checking publication readiness; the fresh clone was never
    exercised (`internal/technical-debt.md`, "Reproduzierbarkeit auf fremdem Rechner").
    """
    if not path.exists():
        pytest.skip(f"{path.name} gehört nicht zur Veröffentlichung — hier nicht prüfbar")
    return path


def test_coding_agent_skills_are_well_formed():
    """`.claude/skills/*/SKILL.md` — the feedforward half of the harness.

    Only name and description stay in context; the body loads on demand. So the
    description is what decides whether a skill is ever opened, and an empty or
    missing one makes the skill invisible.
    """
    skills = sorted(_unpublished_or_skip(ROOT / ".claude" / "skills").glob("*/SKILL.md"))
    assert skills, "keine Coding-Agent-Skills — die Guide-Hälfte fehlt (H1b)"
    problems = []
    for path in skills:
        text = path.read_text(errors="replace")
        if not text.startswith("---"):
            problems.append(f"{path.parent.name}: kein Frontmatter")
            continue
        head = text.split("---", 2)[1]
        name = re.search(r"^name:\s*(\S+)", head, re.MULTILINE)
        desc = re.search(r"^description:\s*(.+)", head, re.MULTILINE)
        if not name or name.group(1) != path.parent.name:
            problems.append(f"{path.parent.name}: name fehlt oder passt nicht zum Ordner")
        if not desc or len(desc.group(1)) < 40:
            problems.append(f"{path.parent.name}: description fehlt oder ist zu knapp")
    assert not problems, "; ".join(problems)


def test_runtime_skills_load_through_the_harness():
    """`skills/*/SKILL.md` — Chester's nine runtime recipes, parsed as the agent parses them.

    Since selmakit 0.1.26 skills are *deferred capabilities* loaded by the harness
    `Skills`, whose frontmatter parser is strict YAML. SelmaKit's old parser was a
    naive `key: value` split and tolerated anything; the harness does not. On the
    upgrade `cross-check` broke the whole gateway at construction — an unquoted
    description containing `checked a second way: sum-of-parts` is a YAML mapping
    inside a mapping. One bad skill takes down every skill, so this runs the real
    loader rather than a regex over the file.
    """
    from pydantic_ai_harness.skills import Skills

    skills_dir = ROOT / "skills"
    names = sorted(
        d.name for d in skills_dir.iterdir() if d.is_dir() and (d / "SKILL.md").is_file()
    )
    assert names, "keine Runtime-Skills in skills/"
    Skills(skills_dir)  # raises ValueError naming the offending SKILL.md


def test_every_capability_appears_in_the_code_map():
    """No age stratification: the oldest capabilities must be documented too.

    Before H3 the five Phase-1/2 classes (`QgisToolboxCapability`,
    `PerceptionCapability`, `VectorCapability`, `GeoValidationCapability`,
    `MapOutputCapability`) appeared **zero** times in 606 lines of code map — what was
    documented was what had been built last, not what mattered most. A new reader needs
    exactly those first.
    """
    code_map = ROOT / "doc" / "code-map.md"
    assert code_map.exists(), "doc/code-map.md fehlt (H3)"
    text = code_map.read_text(errors="replace")
    missing = sorted(
        {node.name for _f, node in _capability_classes()}
        - {n for n in {node.name for _f, node in _capability_classes()} if n in text}
    )
    assert not missing, f"Capability fehlt in der Code-Map: {missing}"


def test_review_subagents_are_well_formed():
    """`.claude/agents/*.md` — the inferential half (H2).

    Narrow by design: several small reviewers beat one broad one, because a broad
    report mixes relevant findings with noise and gets skimmed. So each definition
    must name its scope in the description, and `.claude/commands/review.md` must
    invoke them — an agent nobody calls reviews nothing.
    """
    agents = sorted(_unpublished_or_skip(ROOT / ".claude" / "agents").glob("*.md"))
    assert agents, "keine Review-Subagenten (H2)"
    command = ROOT / ".claude" / "commands" / "review.md"
    assert command.exists(), "kein /review-Befehl — die weiche Verdrahtung fehlt"
    invoked = command.read_text(errors="replace")
    problems = []
    for path in agents:
        text = path.read_text(errors="replace")
        if not text.startswith("---"):
            problems.append(f"{path.stem}: kein Frontmatter")
            continue
        head = text.split("---", 2)[1]
        name = re.search(r"^name:\s*(\S+)", head, re.MULTILINE)
        desc = re.search(r"^description:\s*(.+)", head, re.MULTILINE)
        if not name or name.group(1) != path.stem:
            problems.append(f"{path.stem}: name fehlt oder passt nicht zum Dateinamen")
        if not desc or len(desc.group(1)) < 40:
            problems.append(f"{path.stem}: description fehlt oder ist zu knapp")
        if path.stem not in invoked:
            problems.append(f"{path.stem}: wird von /review nicht aufgerufen")
    assert not problems, "; ".join(problems)


def test_the_single_check_entrypoint_exists():
    """One definition of "green": the hook, the human and the agent call `./check.sh`.

    Without it three definitions drift apart — the hook's, CI's, and the one in the
    developer's head.
    """
    script = ROOT / "check.sh"
    assert script.exists(), "check.sh fehlt — der eine Prüfbefehl (H1d)"
    assert script.stat().st_mode & 0o111, "check.sh ist nicht ausführbar"
    claude_md = _unpublished_or_skip(ROOT / "CLAUDE.md")
    assert "check.sh" in claude_md.read_text(errors="replace"), (
        "check.sh ist nicht in CLAUDE.md unter 'Tooling & commands' eingetragen — "
        "ein Prüfbefehl, den der Agent nicht kennt, wird nicht gerufen."
    )


@lru_cache(maxsize=1)
def _mypy_errors_per_file_cached() -> tuple[tuple[str, int], ...]:
    return tuple(sorted(_mypy_errors_per_file().items()))


def _mypy_errors_per_file() -> dict[str, int]:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    counts: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^([\w/.]+\.py):\d+: error:", line)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def test_clean_modules_stay_type_clean():
    """The strict half: a module at zero today may never regress.

    Split rather than one global number, because a single total lets a fix in one
    file pay for a new error in another — the count stays put while the code gets
    worse where it was clean.
    """
    clean = _load_baseline().get("mypy_clean")
    if not clean:
        pytest.skip("keine mypy-Baseline eingecheckt")
    counts = dict(_mypy_errors_per_file_cached())
    broken = {f: counts[f] for f in clean if counts.get(f)}
    assert not broken, (
        f"typsaubere Module mit neuen Fehlern: {broken}. Diese Dateien sind scharf "
        "gestellt — beheben, nicht die Baseline nachziehen."
    )


def test_typed_errors_do_not_grow():
    """The ratchet half: the modules that carry debt may not carry more."""
    baseline = _load_baseline().get("mypy_errors")
    if baseline is None:
        pytest.skip("keine mypy-Baseline eingecheckt")
    counts = dict(_mypy_errors_per_file_cached())
    grown = {f: (n, baseline.get(f, 0)) for f, n in counts.items() if n > baseline.get(f, 0)}
    assert not grown, (
        f"mehr Typfehler als in der Baseline (jetzt, vorher): {grown}. Beheben — oder, "
        "wenn unvermeidbar, die Baseline mit "
        "`uv run python tests/test_structure.py --update-baseline` neu setzen; der "
        "neue Wert steht dann im Diff."
    )


def _refuse_to_unsharpen(counts: dict[str, int]) -> None:
    """Stop `--update-baseline` from quietly retiring a type-clean file.

    The clean list is the one ratchet whose message is an instruction —
    *"beheben, nicht die Baseline nachziehen"* — and prose does not bind. On
    2026-08-19 a new error in `chester/capabilities/citymodel.py` was written into
    the baseline by a routine update, which both recorded the error **and** struck
    the file from the clean list; the guard that existed for exactly this case
    reported the problem and was then overwritten by the fix-it command. So the
    updater now refuses, and says which file and what to do.
    """
    clean = set(_load_baseline().get("mypy_clean") or [])
    unsharpened = sorted(f for f in clean if counts.get(f))
    if unsharpened:
        print(
            "Baseline NICHT geschrieben — diese Dateien sind typsauber gestellt und "
            f"haben jetzt Fehler: {unsharpened}\n"
            "Den Fehler beheben. Soll eine Datei die Schärfung wirklich verlieren, "
            "den Eintrag von Hand aus `mypy_clean` nehmen — dann steht die "
            "Entscheidung im Diff, statt in einem Werkzeugaufruf zu verschwinden.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _write_baseline() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format=concise", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    total = len([ln for ln in proc.stdout.splitlines() if ": " in ln and ".py:" in ln])
    ruff_files = _ruff_findings_per_file()
    counts = _mypy_errors_per_file()
    _refuse_to_unsharpen(counts)
    # Every file the ratchet checks, not just the package: since 2026-08-19 the
    # scope is the whole repo, and 42 findings had accumulated in `test_app.py`,
    # `evals.py`, `testprompt.py` and `tests/` precisely because nothing looked
    # there. `_line_counts` already knows the published file set.
    clean = sorted(f for f in _line_counts() if not counts.get(f))
    BASELINE.write_text(
        json.dumps(
            {
                "file_lines": dict(sorted(_line_counts().items())),
                "ruff_total": total,
                "ruff_files": dict(sorted(ruff_files.items())),
                "mypy_clean": clean,
                "mypy_errors": dict(sorted(counts.items())),
            },
            indent=1,
        )
        + "\n"
    )
    print(f"Baseline geschrieben: {len(_line_counts())} Dateien, {total} Lint-Befunde")


if __name__ == "__main__":
    if "--update-baseline" in sys.argv:
        _write_baseline()


# ── one capability set, one place ────────────────────────────────────────────

_GATEWAY_CALLERS = ("gateway.py", "ask.py", "testprompt.py", "evals.py", "test_app.py")


def test_every_gateway_call_uses_chesters_capability_set():
    """`Gateway.from_config` without `capabilities=` silently restores the defaults.

    The set is filtered in one place (`agent_build.selmakit_capabilities`, today it
    drops `CronCapability`). A call site that forgets the argument gets a *different
    agent* than the rest — the bench would then measure something the gateway never
    runs, which is exactly the drift `agent_build` exists to prevent.
    """
    missing = []
    for rel in _GATEWAY_CALLERS:
        text = (ROOT / rel).read_text(errors="replace")
        for call in re.finditer(r"Gateway\.from_config\((.*?)\)", text, re.S):
            args = call.group(1)
            if "STATE_DIR" not in args:  # a docstring's `from_config(...)`, not a call
                continue
            if "capabilities=selmakit_capabilities" not in args:
                missing.append(rel)
    assert not missing, (
        "Gateway.from_config ohne `capabilities=selmakit_capabilities`: "
        + ", ".join(sorted(set(missing)))
    )


def test_the_dropped_capabilities_are_named_with_a_reason():
    """A silently shrinking capability set is a trap; the drop list carries its why."""
    src = (ROOT / "agent_build.py").read_text(errors="replace")
    assert "_DROPPED_SELMAKIT_CAPABILITIES" in src
    head = src[: src.index("_DROPPED_SELMAKIT_CAPABILITIES")]
    comment = head[head.rindex("\n\n") :]
    assert "#" in comment and len(comment) > 120, (
        "zur Streichliste fehlt die Begründung im Code — ohne sie ist beim nächsten "
        "SelmaKit-Update nicht entscheidbar, ob der Eintrag noch gilt"
    )


def test_the_two_version_numbers_agree():
    """`chester.__version__` und `pyproject.toml` müssen dieselbe Zahl nennen.

    Sie taten es drei Vorabversionen lang nicht: `pyproject` zählte auf 0.1.2 weiter,
    während das Paket weiter 0.1.0 meldete. Ein Nutzer, der die Version zur Laufzeit
    abfragt — die einzige Stelle, an der sie *im Betrieb* sichtbar ist — bekam eine
    falsche Auskunft, und keine Prüfung sagte etwas dazu.
    """
    import tomllib

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    src = (ROOT / "chester" / "__init__.py").read_text(errors="replace")
    module = re.search(r'__version__\s*=\s*"([^"]+)"', src)
    assert module and module.group(1) == declared, (
        f"Versionen weichen ab: chester/__init__.py nennt "
        f"{module.group(1) if module else 'keine'}, pyproject.toml {declared}. "
        "Beim Versionssprung beide setzen."
    )
