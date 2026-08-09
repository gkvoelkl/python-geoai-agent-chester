"""Slash-command tests for the GeoCache/connector commands (offline, no agent)."""

from __future__ import annotations

import asyncio
import types

import geopandas as gpd
from shapely.geometry import box

import agent_build
from chester.geocache import GeoCache


class _FakeAgent:
    """Captures @agent.command handlers without a real SelmaKit agent."""

    def __init__(self):
        self.handlers = {}

    def command(self, path):
        def deco(fn):
            self.handlers[path] = fn
            return fn

        return deco


def _ctx(args=""):
    return types.SimpleNamespace(args=args, session_key="t", session=None, agent=None)


def _run(coro):
    return asyncio.run(coro)


def _register(workspace, monkeypatch, roots=()):
    monkeypatch.setattr(
        agent_build, "_load_geodata",
        lambda: {"roots": list(roots), "postgis": None, "stac_catalogs": None},
    )
    agent = _FakeAgent()
    agent_build.register_geo_commands(agent, workspace_dir=str(workspace))
    return agent


def _write(path, **kw):
    gpd.GeoDataFrame(
        {"n": ["a", "b"][: kw.get("rows", 1)]},
        geometry=[box(0, 0, 1, 1), box(2, 2, 3, 3)][: kw.get("rows", 1)],
        crs="EPSG:25832",
    ).to_file(path, **{k: v for k, v in kw.items() if k != "rows"})


def test_geocache_lists_and_removes_a_cache_copy(tmp_path, monkeypatch):
    cache = tmp_path / "geocache"
    cache.mkdir()
    _write(cache / "buf.geojson")
    GeoCache(workspace=str(tmp_path)).sync()

    agent = _register(tmp_path, monkeypatch)
    listing = _run(agent.handlers["/geocache"](_ctx()))
    assert "**GeoCache**" in listing and "geocache/buf.geojson" in listing

    removed = _run(agent.handlers["/geocache"](_ctx("rm geocache/buf.geojson")))
    assert "Removed" in removed and not (cache / "buf.geojson").exists()


def test_geocache_rm_refuses_user_source(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    _write(root / "master.geojson")
    ws = tmp_path / "ws"
    ws.mkdir()

    agent = _register(ws, monkeypatch, roots=[str(root)])
    out = _run(agent.handlers["/geocache"](_ctx("rm master.geojson")))
    assert "source: user" in out
    assert (root / "master.geojson").exists()  # the master is never deleted


def test_geocache_prune_dry_run_keeps_everything(tmp_path, monkeypatch):
    cache = tmp_path / "geocache"
    cache.mkdir()
    _write(cache / "x.geojson")
    GeoCache(workspace=str(tmp_path)).sync()

    agent = _register(tmp_path, monkeypatch)
    out = _run(agent.handlers["/geocache"](_ctx("prune --dry-run")))
    assert "nothing is expired" in out
    assert (cache / "x.geojson").exists()


def test_geoconnector_and_geodataset(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    _write(root / "house.gpkg", layer="parcels", driver="GPKG", rows=2)
    ws = tmp_path / "ws"
    ws.mkdir()

    agent = _register(ws, monkeypatch, roots=[str(root)])
    conn = _run(agent.handlers["/geoconnector"](_ctx()))
    assert "house.gpkg" in conn and "container/file" in conn

    datasets = _run(agent.handlers["/geodataset"](_ctx(str(root / "house.gpkg"))))
    assert "parcels" in datasets and "2 feat" in datasets


def test_geodataset_without_connector_shows_usage(tmp_path, monkeypatch):
    agent = _register(tmp_path, monkeypatch)
    out = _run(agent.handlers["/geodataset"](_ctx()))
    assert "Usage" in out
