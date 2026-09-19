"""Test-Level 1: the tool and capability counts in doc/ match the code.

The counts were rewritten by hand after every change and drifted every time: on
2026-09-19 `doc/code-map.md` alone said 81, 82 and (implicitly) 83 MCP tools, and 19
capabilities where the table had 21 rows; `usage.md` recorded two earlier wrong
figures of its own. Prose cannot keep a number current, so this test does. Each
number now lives in exactly one place, and that place is compared with the live
count here. The QGIS-on figures are not checked — they need a QGIS install.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CODE_MAP = ROOT / "doc" / "code-map.md"
USAGE = ROOT / "doc" / "usage.md"


def _find(pattern: str, path: Path) -> tuple[int, ...]:
    match = re.search(pattern, path.read_text(encoding="utf-8"))
    assert match, f"{path.name}: the counting sentence /{pattern}/ is gone — restore or adapt"
    return tuple(int(g) for g in match.groups())


def _tool_names(capabilities: list) -> set[str]:
    names: set[str] = set()
    for cap in capabilities:
        tools = getattr(cap.get_toolset(), "tools", None)
        if isinstance(tools, dict):
            names |= set(tools)
    return names


@pytest.fixture
def without_qgis(monkeypatch):
    import agent_build

    monkeypatch.setattr(agent_build, "qgis_available", lambda: False)
    caps = agent_build.geo_capabilities()
    return len(caps), len(_tool_names(caps))


def test_capability_class_count_in_the_code_map_heading():
    classes = sum(
        len(re.findall(r"^class \w+Capability\(", p.read_text(encoding="utf-8"), re.M))
        for p in [*(ROOT / "packages" / "chester-agent" / "chester" / "capabilities").glob("*.py"),
                  *(ROOT / "packages" / "chester-runtime" / "chester" / "runtime").glob("*.py")]
    )
    (documented,) = _find(r"## Die (\d+) Capabilities auf einen Blick", CODE_MAP)
    assert documented == classes, f"code-map says {documented} capabilities, code has {classes}"


def test_runtime_counts_without_qgis_in_the_code_map(without_qgis):
    documented = _find(r"\*\*(\d+) / (\d+) ohne QGIS", CODE_MAP)
    assert documented == without_qgis, (
        f"code-map says {documented[0]} capabilities / {documented[1]} tools without "
        f"QGIS, geo_capabilities() yields {without_qgis[0]} / {without_qgis[1]}"
    )


def test_runtime_counts_without_qgis_in_usage(without_qgis):
    documented = _find(r"\*\*(\d+) Fähigkeiten und (\d+) Werkzeuge\s+ohne QGIS", USAGE)
    assert documented == without_qgis


def test_mcp_catalogue_count_in_usage(without_qgis):
    from chester import mcpserver

    served = len(mcpserver.collect_tools("/tmp/chester-doc-counts"))
    mcp, agent_tools = _find(r"Meldet \*\*(\d+) Werkzeuge\*\* an: dieselben (\d+)", USAGE)
    assert mcp == served, f"usage.md says {mcp} MCP tools, the server registers {served}"
    assert agent_tools == without_qgis[1]
