"""Ask the local model server whether a model can take an image in the prompt.

`inspect_map` hands its snapshot to the main model as `BinaryContent` and tells a
text-only model to call back with ``via_vision_model=True``. That instruction can never
arrive: the server rejects the whole *request* (Ollama: HTTP 400, llama.cpp: HTTP 500
"image input is not supported") before the model reads a token, the exception aborts
the stream, and — SelmaKit persists a session only for a completed run — the turn
leaves no trace. Observed 2026-08-19 (Ollama, `walk-isochrone-hauptbahnhof`, 634 s of
correct work lost) and 2026-11-07 (llama.cpp, qwen3.6-coder, `bayerwald-heights-dgm1`).

So the routing is decided *before* the image is attached, here and not by the model.
The probe is timid: ``False`` only when the server states it, else ``None`` and the
caller attaches the image as before. Wrong that way costs one hop to the fallback
vision model; wrong the other way costs the run. Supersedes the "assume it can see"
half of decision C in `doc/visual-validation.md`. Pure stdlib, one short HTTP call.
"""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://localhost:11434/v1"

# Probing sits in front of a tool call, so it must not become the slow part; a
# local server answers in milliseconds or it is not the one we are talking to.
_TIMEOUT_S = 3.0

# Per process: a running model's capability list does not change under us.
_cache: dict[tuple[str, str], bool | None] = {}


def _native_api_root(base_url: str) -> str:
    """Ollama's own API root, derived from the OpenAI-compatible ``base_url``.

    The config points at ``…:11434/v1`` (what SelmaKit talks to); ``/api/show``
    lives one level up, on the native API.
    """
    url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    return url[: -len("/v1")] if url.endswith("/v1") else url


def _probe(root: str, name: str) -> bool | None:
    """Ask ``/api/show`` for the model's capabilities. ``None`` = no usable answer."""
    request = Request(
        f"{root}/api/show",
        data=json.dumps({"model": name}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310  # config'd host
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        # An older server states nothing here. Silence is not a denial — the
        # caller must fall back to the old behaviour, not to "cannot see".
        return None
    return "vision" in capabilities


def _probe_llamacpp(base_url: str, name: str) -> bool | None:
    """llama.cpp's router lists each model's start arguments: ``--no-mmproj`` means no
    image input (``False``); anything else stays ``None``."""
    url = (base_url or "").strip().rstrip("/")
    try:
        with urlopen(Request(f"{url}/models"), timeout=_TIMEOUT_S) as response:  # noqa: S310
            models = json.loads(response.read().decode("utf-8", "replace")).get("data", [])
    except (OSError, ValueError, AttributeError):
        return None
    for m in models:
        if m.get("id") == name:
            args = (m.get("status") or {}).get("args") or []
            return False if "--no-mmproj" in args or "--no-mmproj-auto" in args else None
    return None


def sees_images(model: str, base_url: str = "") -> bool | None:
    """Can ``model`` take an image part in a prompt? ``True`` / ``False`` / ``None``.

    ``None`` means *unknown* and must be read as "carry on as before", never as
    ``False``. Probed: a local Ollama (``ollama/<name>``, its capability list) and a
    llama.cpp router (``local/<name>``, ``--no-mmproj`` in its start arguments); every
    hosted provider answers ``None``, there the caller's own error handling is the
    cheaper guard. Capability lists move with the server: on 2026-09-04 Ollama newly
    reported ``vision`` for ``gemma4:26b-mlx`` (and the model really saw), where it had
    not on 2026-08-19 — **re-measure rather than trust this paragraph**.
    """
    provider, _, name = (model or "").strip().partition("/")
    if provider == "local" and name:
        key = (base_url, name)
        if key not in _cache:
            _cache[key] = _probe_llamacpp(base_url, name)
        return _cache[key]
    if provider != "ollama" or not name:
        return None
    root = _native_api_root(base_url)
    key = (root, name)
    if key not in _cache:
        _cache[key] = _probe(root, name)
    return _cache[key]
