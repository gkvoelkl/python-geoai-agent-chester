"""NoneArgGuardCapability — the word "None" for an omitted parameter becomes a real null.

Measured 2026-10-25 (Nemotron 3.5 Lightning, `buffer-schools-500m`): the model filled
every optional parameter it did not want with the *string* ``"None"`` —
``render_map(column="None", scheme="None")`` failed with "column 'None' not found",
``sanity_check_result(expected_geometry="None", magnitude="None")`` checked for a geometry
type called "None". For a ``str | None`` parameter the string is valid, so validation
lets it through and the tool sees a word instead of "not given".

**No tools, no instructions** — it rewrites arguments before validation, where a model
cannot be argued with. Only parameters whose schema admits null are touched: there the
word cannot be meant literally. A parameter typed plain ``str`` keeps whatever it gets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic_ai.capabilities import AbstractCapability

#: Spellings of "no value" a model writes as a string.
NULL_WORDS = frozenset({"None", "none", "null", "Null", "NULL"})


def nullable_params(schema: dict) -> frozenset[str]:
    """Names of the parameters in a JSON schema that admit null."""
    out = set()
    for name, prop in (schema.get("properties") or {}).items():
        kinds = [prop, *(prop.get("anyOf") or []), *(prop.get("oneOf") or [])]
        types = [k.get("type") for k in kinds]
        if "null" in types or any(isinstance(t, list) and "null" in t for t in types):
            out.add(name)
    return frozenset(out)


def nulled(args: dict, nullable: frozenset[str]) -> dict:
    """``args`` with every null word in a nullable parameter replaced by None."""
    return {k: (None if k in nullable and isinstance(v, str) and v.strip() in NULL_WORDS
                else v) for k, v in args.items()}


@dataclass
class NoneArgGuardCapability(AbstractCapability[Any]):
    """Turns ``"None"``/``"null"`` in a nullable parameter into None, for every tool."""

    def get_instructions(self):
        """Nothing — the correction happens in the arguments, not in the prompt."""
        return None

    async def before_tool_validate(self, ctx, *, call, tool_def, args):
        nullable = nullable_params(tool_def.parameters_json_schema or {})
        if not nullable:
            return args
        if isinstance(args, dict):
            return nulled(args, nullable)
        if isinstance(args, str):  # raw JSON text from the provider
            try:
                parsed = json.loads(args)
            except ValueError:
                return args
            if isinstance(parsed, dict):
                return json.dumps(nulled(parsed, nullable))
        return args
