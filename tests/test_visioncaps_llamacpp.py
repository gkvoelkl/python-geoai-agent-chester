"""`sees_images` for a llama.cpp router (``local/<name>``) — no server, a stubbed answer.

Test-Level 1. Found 2026-11-07: with qwen3.6-coder as main model (started with
``--no-mmproj``), `inspect_map` attached its snapshot to the main model, llama.cpp
answered HTTP 500 "image input is not supported", and the run died without a session.
`sees_images` only probed Ollama and said "unknown" for every ``local/`` model, so the
routing to the configured vision model never happened.
"""

from __future__ import annotations

import io
import json

import pytest

from chester import visioncaps

ROUTER = {"data": [
    {"id": "qwen3.6-coder", "status": {"value": "loaded",
                                       "args": ["llama", "serve", "--no-mmproj", "-c", "65536"]}},
    {"id": "gemma4", "status": {"value": "unloaded", "args": ["llama", "serve", "-c", "65536"]}},
]}


@pytest.fixture(autouse=True)
def router(monkeypatch):
    visioncaps._cache.clear()
    calls: list[str] = []

    def fake_urlopen(request, timeout=0):
        calls.append(request.full_url)
        return io.BytesIO(json.dumps(ROUTER).encode())

    monkeypatch.setattr(visioncaps, "urlopen", fake_urlopen)
    yield calls
    visioncaps._cache.clear()


def test_a_model_without_its_projector_cannot_see(router):
    assert visioncaps.sees_images("local/qwen3.6-coder", "http://localhost:9931/v1") is False
    assert router == ["http://localhost:9931/v1/models"]


def test_a_model_with_a_projector_stays_unknown():
    """Timid on purpose: no stated denial, no "cannot see" — the old behaviour stays."""
    assert visioncaps.sees_images("local/gemma4", "http://localhost:9931/v1") is None
    assert visioncaps.sees_images("local/not-listed", "http://localhost:9931/v1") is None


def test_an_unreachable_router_is_unknown(monkeypatch):
    def down(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(visioncaps, "urlopen", down)
    assert visioncaps.sees_images("local/qwen3.6-coder", "http://localhost:9931/v1") is None


def test_the_answer_is_cached_per_process(router):
    for _ in range(3):
        visioncaps.sees_images("local/qwen3.6-coder", "http://localhost:9931/v1")
    assert len(router) == 1
