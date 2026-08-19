"""The probe that keeps a snapshot away from a model that cannot take one.

From the incident of 2026-08-19: `inspect_map` attached its PNG to
`gemma4:26b-mlx`, Ollama answered the *request* with HTTP 400 ("this model does
not support image input"), and the run died 634 s in — after correct
geoprocessing, and with no session persisted to read back. The tool's own escape
hatch ("call again with via_vision_model=True") was unreachable, because a model
rejected at the transport layer never gets to say anything.

What these pin down is the *direction of the doubt*: `sees_images` may only ever
answer ``False`` when the server explicitly states a capability list without
``vision``. Every other outcome is ``None`` — unknown — and the caller keeps
behaving as it always did. An over-eager ``False`` costs a needless hop to the
fallback vision model; a wrong ``True`` costs the whole run.
"""

from __future__ import annotations

import io
import json

import pytest

from chester import visioncaps


@pytest.fixture(autouse=True)
def _clear_cache():
    """The answer is cached per process — each test must start from cold."""
    visioncaps._cache.clear()
    yield
    visioncaps._cache.clear()


def _serve(monkeypatch, payload, *, record=None):
    """Stub `/api/show` with ``payload`` (a dict, or an exception to raise)."""

    def fake_urlopen(request, timeout=None):
        if record is not None:
            record.append((request.full_url, json.loads(request.data.decode())))
        if isinstance(payload, Exception):
            raise payload
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(visioncaps, "urlopen", fake_urlopen)


def test_a_stated_capability_list_without_vision_is_a_no(monkeypatch):
    _serve(monkeypatch, {"capabilities": ["completion", "tools", "thinking"]})
    assert visioncaps.sees_images("ollama/gemma4:26b-mlx") is False


def test_a_stated_vision_capability_is_a_yes(monkeypatch):
    _serve(monkeypatch, {"capabilities": ["completion", "vision", "tools"]})
    assert visioncaps.sees_images("ollama/qwen3-vl:latest") is True


def test_a_server_that_states_nothing_leaves_us_unknown(monkeypatch):
    """Silence is not a denial — an older Ollama has no `capabilities` key."""
    _serve(monkeypatch, {"model_info": {}})
    assert visioncaps.sees_images("ollama/some-model") is None


def test_an_unreachable_server_leaves_us_unknown(monkeypatch):
    _serve(monkeypatch, OSError("connection refused"))
    assert visioncaps.sees_images("ollama/gemma4:26b-mlx") is None


def test_a_hosted_provider_is_never_probed(monkeypatch):
    """Only a local Ollama answers this question; hosted models are not asked."""
    calls: list = []
    _serve(monkeypatch, {"capabilities": []}, record=calls)
    assert visioncaps.sees_images("anthropic/claude-opus-4-8") is None
    assert visioncaps.sees_images("") is None
    assert not calls


def test_the_probe_hits_the_native_api_next_to_the_openai_endpoint(monkeypatch):
    """The config points at `…/v1`; `/api/show` lives one level up."""
    calls: list = []
    _serve(monkeypatch, {"capabilities": ["vision"]}, record=calls)
    visioncaps.sees_images("ollama/qwen3-vl:latest", "http://localhost:11434/v1")
    assert calls == [("http://localhost:11434/api/show", {"model": "qwen3-vl:latest"})]


def test_the_answer_is_cached_per_model(monkeypatch):
    """This sits in front of a tool call, so it must not re-probe on every snapshot."""
    calls: list = []
    _serve(monkeypatch, {"capabilities": ["completion"]}, record=calls)
    assert visioncaps.sees_images("ollama/gemma4:26b-mlx") is False
    assert visioncaps.sees_images("ollama/gemma4:26b-mlx") is False
    assert len(calls) == 1
