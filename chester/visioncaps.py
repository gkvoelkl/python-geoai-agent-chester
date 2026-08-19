"""Ask the local model server whether a model can take an image in the prompt.

`inspect_map` hands its snapshot to the main model as `BinaryContent` and tells a
text-only model to call back with ``via_vision_model=True``. That instruction can
never arrive: Ollama rejects the whole *request* with HTTP 400 ("this model does
not support image input") before the model reads a single token, the exception
aborts the event stream, and — because SelmaKit persists a session only for a
completed run — the turn leaves no trace behind. Observed 2026-08-19 on
`walk-isochrone-hauptbahnhof`: 634 s of correct geoprocessing (OSM network,
reprojection, service area, map), then nothing to read back and nothing to grade.
The fallback was unreachable for exactly the models it was built for.

So the routing decision is made *before* the image is attached, and it is made
here rather than by the model. The probe is deliberately timid: it answers
``False`` only when the server states a capability list that has no ``vision`` in
it. No list, another provider, no answer at all → ``None``, and the caller keeps
attaching the image as it always did. Being wrong that way costs one needless hop
to the fallback vision model; being wrong the other way costs the run.

This supersedes the "assume it can see" half of decision C in
`doc/visual-validation.md` — the assumption held only where a blind model could
*answer*, and against a local Ollama it cannot.

Pure stdlib: no SelmaKit, no pydantic-ai, one short HTTP call.
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


def sees_images(model: str, base_url: str = "") -> bool | None:
    """Can ``model`` take an image part in a prompt? ``True`` / ``False`` / ``None``.

    ``None`` means *unknown* and must be read as "carry on as before", never as
    ``False``. Only a local Ollama (``ollama/<name>``, SelmaKit's provider prefix)
    is probed; every hosted provider answers ``None``, since there the caller's own
    error handling is the cheaper guard.

    Measured 2026-08-19 on this machine: ``gemma4:26b-mlx`` →
    ``['completion', 'tools', 'thinking']`` (no vision), ``qwen3-vl:latest`` →
    ``[…, 'vision', …]``.
    """
    provider, _, name = (model or "").strip().partition("/")
    if provider != "ollama" or not name:
        return None
    root = _native_api_root(base_url)
    key = (root, name)
    if key not in _cache:
        _cache[key] = _probe(root, name)
    return _cache[key]
