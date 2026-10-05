"""ToolUsageCapability — writes every tool call into the tool-usage ledger.

**No tools, no instructions**, like `RunLogCapability`: an observer the model does not
know about. The ledger itself and the reasons for it are in `chester/toolusage.py`.

Kept apart from the run log on purpose. The run log is one file per session key and
holds arguments and results — the account of *one* run. The ledger is one file for
all runs and holds only tool, time and outcome — the account of *the toolbox*. Merging
them would make either the per-run file or the long-term file the wrong shape.

The three hooks follow the run log's contract: ``after_tool_execute`` returns its
result unchanged, and both error hooks **re-raise** — a return value there would
replace the error or the validated arguments.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability

from chester import toolusage


@dataclass
class ToolUsageCapability(AbstractCapability[Any]):
    """Appends one ledger line per finished, failed or refused tool call."""

    log_dir: str = toolusage.DEFAULT_LOG_DIR
    #: For agents without a session key in ``deps`` — chester-team's ressort agents.
    session: str | None = None
    _started: dict[str, float] = field(default_factory=dict, repr=False)

    def get_instructions(self):
        """Nothing — an observer the model has no business knowing about."""
        return None

    def _record(self, ctx: RunContext[Any], tool_def, outcome: str, call=None) -> None:
        started = self._started.pop(getattr(call, "tool_call_id", "") or "", None)
        seconds = round(time.monotonic() - started, 2) if started else None
        toolusage.record(getattr(tool_def, "name", "?"), outcome, seconds=seconds,
                         session=self.session if ctx.deps is None else ctx.deps,
                         log_dir=self.log_dir)

    async def before_tool_execute(self, ctx: RunContext[Any], *, call, tool_def, args):
        self._started[getattr(call, "tool_call_id", "") or ""] = time.monotonic()
        return args

    async def after_tool_execute(self, ctx: RunContext[Any], *, call, tool_def, args, result):
        self._record(ctx, tool_def, toolusage.outcome_of(result), call)
        return result

    async def on_tool_execute_error(self, ctx: RunContext[Any], *, call, tool_def, args, error):
        """**Must re-raise** — returning would turn the error into a tool result."""
        self._record(ctx, tool_def, "error", call)
        raise error

    async def on_tool_validate_error(self, ctx: RunContext[Any], *, call, tool_def, args, error):
        """**Must re-raise** — returning would count as validated arguments."""
        self._record(ctx, tool_def, "invalid")
        raise error
