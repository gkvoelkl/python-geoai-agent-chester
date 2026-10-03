"""Every pure-Python geo operation is pinned by at least one *value*, not just a contract.

The gap this closes was measured on 2026-09-22. The four modules were well tested —
refusals, warnings, provenance, geometry preservation, "voll rein, leer raus" — and six
of the twenty-two operations had no test that ever compared a result to a number.
`buffer` was the worst of them: five tests called it, all five were refusals, and not
one asked whether the circle it returns is a circle.

That is not a cosmetic gap. A contract test reads `ok: true` and is satisfied; only an
expected number sees a wrong one. `field_sum` summed a text column by concatenating it
— `"10" + "20" + "30"` is `"102030"`, which `float()` accepts — and reported 102030.0
instead of 60.0 under `ok: true` for as long as the column arrived from `join`. The
existing refusal test passed throughout, because it used text that does not parse as a
number. The defect surfaced the moment an expected value was written down.

So this test is the ratchet: a new operation arrives with an expected number, or the
suite goes red. What counts is deliberately broad — a comparison of some part of the
result against a literal, a list, an expression, or through `allclose`/`approx` — so
the rule cannot be satisfied by `assert res["ok"] is True` alone, and is not fussy
about how the number is written.
"""

from __future__ import annotations

import ast
import collections
import importlib
import pathlib

import pytest

#: The four pure cores. Anything callable and public in them is an operation.
MODULES = ("geoops", "networkops", "rasterops", "terrainops")

#: Public names in those modules that are not operations.
NOT_AN_OPERATION = {"LayerNotFound", "grass_available"}

#: Helpers whose presence in an assert means a number is being checked.
NUMERIC_HELPERS = {"allclose", "isclose", "approx", "nansum", "median", "sum",
                   "array_equal"}

#: Result keys that every operation returns and that a *contract* test checks anyway.
#: `res["features_out"] == 1` says the step ran, not that it computed the right thing —
#: a buffer of the wrong radius, a reprojection into the wrong CRS and a dissolve that
#: only groups all return one feature. Counting them would let the ratchet pass on
#: tests that never look at the result, which is exactly what it exists to prevent
#: (verified by removing the two real assertions from the buffer test: with these keys
#: counted, the suite stayed green).
BOOKKEEPING_KEYS = {"features_in", "features_out", "ok"}

TESTS = pathlib.Path(__file__).parent


def _operations() -> dict[str, str]:
    ops = {}
    for name in MODULES:
        module = importlib.import_module(f"chester.{name}")
        for attr, value in vars(module).items():
            if (callable(value) and not attr.startswith("_")
                    and getattr(value, "__module__", "").endswith(name)
                    and attr not in NOT_AN_OPERATION):
                ops[attr] = name
    return ops


def _aliases(tree: ast.AST) -> set[str]:
    """The names the four modules are reachable under in this test file.

    Needed because `.join(` is also `str.join` and `os.path.join`: without binding the
    call to the module, a path-building helper would count as a test of `geoops.join`.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("chester"):
            out |= {a.asname or a.name for a in node.names if a.name in MODULES}
    return out


def _is_bookkeeping(node: ast.AST) -> bool:
    """``res["features_out"]`` and friends — present in every return, proves nothing."""
    return (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
            and node.slice.value in BOOKKEEPING_KEYS)


def _pins_a_value(part: ast.AST) -> bool:
    """One expression inside an assert: does it hold a result against a number?"""
    if (isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute)
            and part.func.attr in NUMERIC_HELPERS):
        return True
    if not isinstance(part, ast.Compare):
        return False
    if _is_bookkeeping(part.left):
        return False
    for op, right in zip(part.ops, part.comparators, strict=False):
        if not isinstance(op, (ast.Eq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            continue
        # A local that holds a computed value counts too: `a = got.area;
        # assert a == 100` pins the same number as the inline form.
        if isinstance(right, (ast.List, ast.Tuple, ast.Set, ast.BinOp)):
            return True
        if (isinstance(right, ast.Constant) and isinstance(right.value, (int, float))
                and not isinstance(right.value, bool)):
            return True
    return False


def _asserts_a_value(fn: ast.FunctionDef) -> bool:
    return any(_pins_a_value(part)
               for node in ast.walk(fn) if isinstance(node, ast.Assert)
               for part in ast.walk(node.test))


def _coverage() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    ops = _operations()
    called: dict[str, set[str]] = collections.defaultdict(set)
    valued: dict[str, set[str]] = collections.defaultdict(set)
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text())
        alias = _aliases(tree)
        if not alias:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
                continue
            has_value = _asserts_a_value(node)
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id in alias and sub.func.attr in ops):
                    called[sub.func.attr].add(node.name)
                    if has_value:
                        valued[sub.func.attr].add(node.name)
    return called, valued


def test_the_auditor_still_finds_the_operations():
    """A guard on the guard: a renamed module would silently empty the check below."""
    ops = _operations()
    assert len(ops) >= 22, f"only {len(ops)} operations found — did a module move?"
    for expected in ("buffer", "service_area", "zonal_stats", "slope"):
        assert expected in ops, expected


def test_every_operation_is_called_by_some_test():
    called, _ = _coverage()
    missing = sorted(op for op in _operations() if not called[op])
    assert not missing, (
        f"operations no test ever calls: {missing}. `extract_by_attribute` sat here "
        "with zero tests until 2026-09-22.")


def test_every_operation_is_pinned_by_an_expected_value():
    called, valued = _coverage()
    missing = sorted(op for op in _operations() if not valued[op])
    assert not missing, (
        f"operations with no expected value in any test: {missing}. Each is called by "
        f"{ {op: len(called[op]) for op in missing} } test(s), but every assertion is "
        "about the contract — `ok`, a warning, a refusal, a provenance file. Add one "
        "test whose expected number is derived by hand (see "
        "`test_dissolve_removes_the_shared_edge` or "
        "`test_the_reachable_set_matches_an_analytic_oracle`).")


@pytest.mark.parametrize("module", MODULES)
def test_no_module_is_left_without_an_analytic_anchor(module):
    """Per module, not just in total — so a well-tested module cannot carry a bare one."""
    _, valued = _coverage()
    ops = [op for op, mod in _operations().items() if mod == module]
    assert ops, f"{module} exports no operation"
    assert all(valued[op] for op in ops), sorted(op for op in ops if not valued[op])
