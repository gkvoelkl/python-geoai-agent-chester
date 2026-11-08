"""`CHESTER_JUDGE=result`: the judge grades the result, never the route.

Test-Level 1 — the judge is a `FunctionModel` that records the prompt it was given and
answers with a fixed verdict.
"""

from __future__ import annotations

import asyncio
import json

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

import judgemode
from benchjudge import JUDGE_SYSTEM, Verdict, judge_run

_TEST = {"id": "t", "success_criteria": ["Puffer in EPSG:25832"], "tools_expected": []}


def _judged_prompt(monkeypatch, mode: str) -> str:
    monkeypatch.setenv(judgemode.ENV, mode)
    seen: list[str] = []

    def model(messages, info):
        seen.append(messages[-1].parts[-1].content)
        verdict = {"criteria": [{"text": "Puffer in EPSG:25832", "passed": True}],
                   "passed": True, "reason": "ok"}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, json.dumps(verdict))])

    judge = Agent(FunctionModel(model), output_type=Verdict, system_prompt=JUDGE_SYSTEM)
    asyncio.run(judge_run(judge, _TEST, "Puffer um Schulen", ["run_code", "vector_buffer"],
                          "Fertig.", scope="vector_buffer(distance=500)",
                          facts="buffers.gpkg: EPSG:25832, 84 features, Polygon"))
    return seen[0]


def test_result_mode_hides_the_route(monkeypatch):
    prompt = _judged_prompt(monkeypatch, "result")
    assert prompt.startswith("# Mode: judge the RESULT only")
    assert "tool-call sequence" not in prompt and "vector_buffer(distance" not in prompt
    assert "buffers.gpkg: EPSG:25832" in prompt and "Fertig." in prompt


def test_default_mode_still_shows_the_route(monkeypatch):
    prompt = _judged_prompt(monkeypatch, "")
    assert "# Mode:" not in prompt
    assert "run_code → vector_buffer" in prompt and "vector_buffer(distance=500)" in prompt


def test_layer_facts_ignores_run_codes_printed_output(tmp_path, monkeypatch):
    """CodeMode puts a program's printed text under `output`; it is not a path.

    Measured 2026-10-24 (chester-code, qwen3.6-coder): the judging crashed with
    `OSError: File name too long` on a `vector_info` dump read as a file name.
    """
    import testprompt

    monkeypatch.setattr(testprompt, "SESSIONS_DIR", tmp_path)
    (tmp_path / "run.json").write_text(json.dumps([{"parts": [
        {"part_kind": "tool-return", "tool_name": "run_code",
         "content": {"output": "Buffer file info: {'ok': True} " * 40}},
    ]}]))
    assert testprompt.layer_facts("run") == ""
