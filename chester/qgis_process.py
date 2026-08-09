"""Thin Python wrapper around the headless ``qgis_process`` CLI.

This is the single execution path for every QGIS operation in Chester. It shells
out to ``qgis_process`` (located + configured by :mod:`chester.qgis_env`) and
parses the JSON it returns. Nothing here imports PyQGIS.

QGIS 4.0 subcommands used:
    qgis_process list --json                 → providers/algorithms catalog
    qgis_process help <id> --json            → parameter + output schema
    qgis_process run <id> --json -           → run, reading {"inputs": …} on stdin
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from chester.qgis_env import QgisEnv, resolve_qgis_env

DEFAULT_TIMEOUT = 600  # seconds; geoprocessing can be slow


class QgisProcessError(RuntimeError):
    """Raised when a ``qgis_process`` invocation fails or returns no JSON."""


class QgisProcess:
    """Stateful wrapper that caches the (expensive) algorithm catalog."""

    def __init__(self, env: QgisEnv | None = None, timeout: int = DEFAULT_TIMEOUT):
        self._env = env or resolve_qgis_env()
        self.timeout = timeout
        self._algorithms: dict[str, dict] | None = None

    # ── low-level invocation ────────────────────────────────────────────

    def _invoke(self, args: list[str], stdin: str | None = None) -> dict[str, Any]:
        cmd = [str(self._env.bin), *args]
        try:
            proc = subprocess.run(
                cmd,
                input=stdin,
                env=self._env.subprocess_env(),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise QgisProcessError(
                f"qgis_process timed out after {self.timeout}s: {' '.join(args)}"
            ) from exc

        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            detail = proc.stderr.strip() or proc.stdout.strip() or "(no output)"
            raise QgisProcessError(
                f"qgis_process {' '.join(args)} failed "
                f"(exit {proc.returncode}): {detail[:500]}"
            ) from exc

    # ── catalog / search ────────────────────────────────────────────────

    def algorithms(self, refresh: bool = False) -> dict[str, dict]:
        """Return ``{algorithm_id: metadata}`` flattened across all providers."""
        if self._algorithms is None or refresh:
            data = self._invoke(["list", "--json"])
            flat: dict[str, dict] = {}
            for provider in (data.get("providers") or {}).values():
                pname = provider.get("name")
                for alg_id, meta in (provider.get("algorithms") or {}).items():
                    entry = dict(meta)
                    entry["provider"] = pname
                    flat[alg_id] = entry
            self._algorithms = flat
        return self._algorithms

    def search(self, keyword: str, limit: int = 25) -> list[dict]:
        """Fuzzy-match algorithms by id, name, description and tags.

        Multi-word keywords match on **all** tokens (in any order), not as one
        literal phrase — so "join attributes location" finds
        ``native:joinattributesbylocation`` ("Join attributes by location")
        even though that exact phrase never appears.

        When *no* algorithm contains every token, fall back to the best partial
        matches — the algorithms sharing the most tokens with the query — rather
        than returning nothing. So a natural, over-described query like "field
        calculator area" still surfaces ``native:fieldcalculator`` (2 of 3
        tokens) instead of an empty list.
        """
        kw = keyword.lower().strip()
        tokens = kw.split()
        if not tokens:
            return []

        # Score every algorithm by how many query tokens it contains.
        scored: list[tuple[int, str, dict]] = []
        for alg_id, meta in self.algorithms().items():
            haystack = " ".join(
                [
                    alg_id,
                    str(meta.get("name", "")),
                    str(meta.get("short_description", "")),
                    " ".join(meta.get("tags") or []),
                ]
            ).lower()
            matched = sum(1 for t in tokens if t in haystack)
            if matched:
                scored.append((matched, alg_id, meta))
        if not scored:
            return []

        # Keep only the best-overlap tier: full AND matches when any exist
        # (strict, unchanged), otherwise the highest partial overlap.
        best = max(matched for matched, _, _ in scored)
        tier = [(alg_id, meta) for matched, alg_id, meta in scored if matched == best]

        # Prefer a whole-phrase hit in id/name over description-only matches.
        tier.sort(key=lambda am: (
            kw not in am[0].lower()
            and kw not in str(am[1].get("name", "")).lower(),
            am[0],
        ))
        return [
            {
                "id": alg_id,
                "name": meta.get("name"),
                "group": meta.get("group"),
                "provider": meta.get("provider"),
                "description": meta.get("short_description"),
            }
            for alg_id, meta in tier
        ][:limit]

    # ── describe ────────────────────────────────────────────────────────

    def describe(self, algorithm_id: str) -> dict[str, Any]:
        """Return a compact parameter + output schema for one algorithm."""
        data = self._invoke(["help", algorithm_id, "--json"])
        details = data.get("algorithm_details") or {}

        def _compact(d: dict) -> dict:
            out = {}
            for name, spec in (d or {}).items():
                t = spec.get("type") or {}
                out[name] = {
                    "description": spec.get("description"),
                    "type": t.get("name") if isinstance(t, dict) else t,
                    "optional": spec.get("optional"),
                    "default": spec.get("default_value"),
                    "acceptable_values": (
                        t.get("acceptable_values") if isinstance(t, dict) else None
                    ),
                }
            return out

        return {
            "id": algorithm_id,
            "name": details.get("name"),
            "description": details.get("short_description"),
            "parameters": _compact(data.get("parameters") or {}),
            "outputs": _compact(data.get("outputs") or {}),
        }

    # ── run ─────────────────────────────────────────────────────────────

    def run(
        self,
        algorithm_id: str,
        parameters: dict[str, Any],
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """Run an algorithm with ``parameters`` and return its ``results`` map.

        A few algorithms (e.g. the network-analysis ``serviceareafrompoint``)
        refuse to run without a QGIS project context; pass ``project_path`` (a
        ``.qgs`` file) and it rides in the JSON payload next to ``inputs``. Most
        algorithms need no project and leave it ``None``.
        """
        payload_obj: dict[str, Any] = {"inputs": parameters}
        if project_path:
            payload_obj["project_path"] = project_path
        payload = json.dumps(payload_obj)
        data = self._invoke(["run", algorithm_id, "--json", "-"], stdin=payload)
        return {
            "id": algorithm_id,
            "results": data.get("results", {}),
            "inputs": data.get("inputs", parameters),
        }


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    qp = QgisProcess()
    print("algorithm count:", len(qp.algorithms()))
    print("search 'buffer':", [h["id"] for h in qp.search("buffer", limit=5)])
    print("describe native:buffer parameters:", list(qp.describe("native:buffer")["parameters"]))
