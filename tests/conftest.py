"""Pytest configuration: markers and opt-in gating for slow/external tests.

By default `uv run pytest` runs the fast unit tests plus the QGIS integration tests
(skipped automatically if qgis_process is absent). Network and LLM tests are opt-in:

    uv run pytest                 # unit + qgis
    uv run pytest --run-network   # also osmnx / STAC tests
    uv run pytest --run-llm       # also Ollama agent tests
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-network", action="store_true", default=False,
                     help="run tests that hit the network (osmnx, STAC)")
    parser.addoption("--run-llm", action="store_true", default=False,
                     help="run tests that call the local Ollama model")


def pytest_configure(config):
    config.addinivalue_line("markers", "qgis: needs a local qgis_process")
    config.addinivalue_line("markers", "network: needs internet access")
    config.addinivalue_line("markers", "llm: needs Ollama and a pulled model")


def pytest_collection_modifyitems(config, items):
    skips = {}
    if not config.getoption("--run-network"):
        skips["network"] = pytest.mark.skip(reason="needs --run-network")
    if not config.getoption("--run-llm"):
        skips["llm"] = pytest.mark.skip(reason="needs --run-llm")
    for item in items:
        for marker, skip in skips.items():
            if marker in item.keywords:
                item.add_marker(skip)
