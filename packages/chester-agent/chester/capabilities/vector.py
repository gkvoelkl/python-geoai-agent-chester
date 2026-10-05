"""VectorCapability — in-memory vector inspection and analysis with GeoPandas.

Complements the QGIS tools: quick attribute/geometry questions and lightweight
overlays that don't warrant a full ``qgis_process`` round trip. geopandas is
imported lazily inside the tools to keep agent startup fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import AgentToolset, FunctionToolset

from chester.runtime.geopython import build_geo_python_run
from chester.vectoroptools import build_tools as op_build_tools
from chester.vectortools import INSTRUCTIONS as vector_instructions
from chester.vectortools import build_tools
from chester.workspace import DEFAULT_WORKSPACE

#: SQL features that make a pandas expression a syntax error. The case that triggered
#: this detection (2026-09-03, `laguna-xs-2.1` on `pluvial-flow-accumulation-tegernheim`):
#: the model wrote `"waterway" IN ('stream', …) AND geometry IS NOT NULL`, got a
#: SyntaxError with a hint about quotes and backticks, followed the hint, failed again —
#: and then fell back on hand-written PyQGIS. The hint was not wrong, it did not fit the
#: error, and that cost more than no hint at all.
#:
#: That SQL gets typed right here is home-made: the rest of the toolbox is shaped by QGIS
#: and SQL (the former `qgis_extract_by_attribute`, `native:extractbyexpression`); this
#: one tool speaks pandas.






@dataclass
class VectorCapability(AbstractCapability[Any]):
    """GeoPandas-backed vector tools (info, attribute filter, overlay)."""

    workspace: str = DEFAULT_WORKSPACE

    def get_instructions(self):
        def _instructions(ctx: RunContext[Any]) -> str:
            return vector_instructions

        return _instructions

    def get_toolset(self) -> AgentToolset[Any] | None:
        ws = self.workspace





        # The escape hatch lives in chester-runtime (shared with chester-team).
        geo_python_run = build_geo_python_run(ws)

        # Die zehn geprüften Operationen stehen in `chester/vectoroptools.py` — dünne Hüllen
        # um `geoops`, ausgelagert, damit diese Datei ihre Baseline hält.
        return FunctionToolset(
            tools=[*build_tools(ws), geo_python_run, *op_build_tools(ws)]
        )
