"""`validate_result` as a **framework-neutral** wrapper — the gate for foreign clients.

Phase KM, step 4. Chester's thesis is not breadth of function but verifiability:
"Correctness is a loop phase", enforced by `chester/gate.py`. Over MCP it becomes
"Correctness is a tool you may call" — the server cannot force a foreign client into
anything.

**That loss is what cell F+MCP measures, not a shortcoming of the implementation**
(`internal/chester-mcp.md` §5, variant 3: self-report per step *plus* an explicit
`validate_result`). Hence the return says `enforced: false`, unmissably, instead of the
tool description feigning obligation.

Its own module and not in `validationtools.py`: those are the *domain* checks (CRS,
topology, plausibility, cross-check) an agent calls mid-work. This is the final check
over the finished result — and it builds on `gate.py`, the one module with special
status.
"""

from __future__ import annotations

from collections.abc import Callable

from chester import gate


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """`validate_result`, an ``workspace`` gebunden."""
    ws = workspace

    def validate_result(paths: list[str], answer: str = "",
                        level: int = gate.DEFAULT_LEVEL) -> dict:
        """Check finished geo results before you report them, and get the findings.

        Call this **last**, after the files exist and you know what you are going to
        say — it checks both. ``paths`` are the files you produced, ``answer`` is the
        text you are about to send.

        Per file: empty layers, a missing CRS, geometry that does not match the
        declared type, index ranges, and at ``level`` 3 a stored area/length column
        against the real geometry plus redundancy. Over the answer: links that point
        nowhere, and claims about files that do not exist.

        Returns ``must_fix: true`` when something is wrong that would make the answer
        untrue, with one entry per finding (``path``, ``check``, ``severity``,
        ``problem``). ``enforced: false`` is part of the answer: nothing here stops
        you from reporting anyway — the decision is yours.

        ``checks_not_run`` names what this cannot see: the checks that need the whole
        run rather than its result.
        """
        try:
            return gate.inspect_result(paths, answer, workspace=ws, level=level)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return [validate_result]
