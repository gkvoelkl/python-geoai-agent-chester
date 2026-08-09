"""GeoCache retention config + background sync (offline; no QGIS, no network).

Covers the two halves of the TTL work: per-source retention overrides resolved
from the ``geodata`` config block, and the daemon-thread periodic sync.
"""

from __future__ import annotations

import json
import threading

from _util import write_point

from chester import geoconfig, provenance
from chester.geocache import DEFAULT_TTL_DAYS, GeoCache, start_periodic_sync


def _downloaded(tmp_path, name: str, source: str):
    """A cached dataset carrying a provenance sidecar with ``source``."""
    p = write_point(tmp_path / name, 7.0, 50.0, "EPSG:25832")
    provenance.write_meta(str(p), source=source, tool="test")
    return p


def _ttl_of(cache: GeoCache, dataset: str, today: str = "2026-01-01") -> int:
    return next(r for r in cache.list(today=today) if r["dataset"] == dataset)["ttl_days"]


# ── config reader ────────────────────────────────────────────────────────────


def test_load_geodata_defaults_when_config_missing(tmp_path):
    gd = geoconfig.load_geodata(str(tmp_path), "nope.json")
    assert gd["roots"] == [] and gd["postgis"] is None
    assert gd["ttl_days"] is None          # → the GeoCache default applies
    assert gd["ttl_by_source"] == {}
    assert gd["sync_interval_hours"] == 0.0  # → no background sync


def test_load_geodata_reads_retention_block(tmp_path):
    (tmp_path / "chester.json").write_text(json.dumps({
        "geodata": {
            "roots": ["/data"],
            "ttl_days": 10,
            "ttl_by_source": {"connector/osm": 3, "connector/*": 7},
            "sync_interval_hours": 6,
        }
    }))
    gd = geoconfig.load_geodata(str(tmp_path), "chester.json")
    assert gd["roots"] == ["/data"] and gd["ttl_days"] == 10
    assert gd["ttl_by_source"] == {"connector/osm": 3, "connector/*": 7}
    assert gd["sync_interval_hours"] == 6.0


def test_load_geodata_drops_unusable_values(tmp_path):
    """One bad entry must not disable the whole data layer."""
    (tmp_path / "chester.json").write_text(json.dumps({
        "geodata": {
            "ttl_days": "not-a-number",
            "ttl_by_source": {"connector/osm": "soon", "connector/wfs": 5, "": 9},
            "sync_interval_hours": -1,
        }
    }))
    gd = geoconfig.load_geodata(str(tmp_path), "chester.json")
    assert gd["ttl_days"] is None                       # unusable → fall back
    assert gd["ttl_by_source"] == {"connector/wfs": 5}   # only the sane entry
    assert gd["sync_interval_hours"] == 0.0             # negative → off


def test_from_config_builds_cache_from_block(tmp_path):
    cfg = tmp_path / "state"
    cfg.mkdir()
    (cfg / "chester.json").write_text(json.dumps({
        "geodata": {"ttl_days": 12, "ttl_by_source": {"connector/osm": 2}}
    }))
    gd = geoconfig.load_geodata(str(cfg), "chester.json")
    cache = GeoCache.from_config(str(tmp_path / "ws"), gd)
    assert cache.default_ttl_days == 12
    assert cache.ttl_by_source == {"connector/osm": 2}


# ── per-source overrides ─────────────────────────────────────────────────────


def test_exact_source_override_applies(tmp_path):
    _downloaded(tmp_path, "osm.geojson", "connector/osm")
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=30,
                     ttl_by_source={"connector/osm": 3})
    assert _ttl_of(cache, "osm.geojson") == 3


def test_wildcard_family_override_applies(tmp_path):
    _downloaded(tmp_path, "wfs.geojson", "connector/wfs")
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=30,
                     ttl_by_source={"connector/*": 7})
    assert _ttl_of(cache, "wfs.geojson") == 7


def test_exact_source_beats_wildcard(tmp_path):
    _downloaded(tmp_path, "osm.geojson", "connector/osm")
    _downloaded(tmp_path, "wfs.geojson", "connector/wfs")
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=30,
                     ttl_by_source={"connector/*": 7, "connector/osm": 3})
    assert _ttl_of(cache, "osm.geojson") == 3   # carved out of the family rule
    assert _ttl_of(cache, "wfs.geojson") == 7


def test_longest_wildcard_prefix_wins(tmp_path):
    _downloaded(tmp_path, "alti.geojson", "connector/swissalti3d")
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=30,
                     ttl_by_source={"connector/*": 7, "connector/swiss*": 14})
    assert _ttl_of(cache, "alti.geojson") == 14


def test_unmatched_source_keeps_default(tmp_path):
    write_point(tmp_path / "own.geojson", 7.0, 50.0, "EPSG:25832")  # source: chester
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=21,
                     ttl_by_source={"connector/*": 7})
    assert _ttl_of(cache, "own.geojson") == 21


def test_sidecar_ttl_beats_source_override(tmp_path):
    p = write_point(tmp_path / "fixed.geojson", 7.0, 50.0, "EPSG:25832")
    provenance.write_meta(str(p), source="connector/osm", tool="test", ttl_days=99)
    cache = GeoCache(workspace=str(tmp_path), ttl_by_source={"connector/osm": 3})
    assert _ttl_of(cache, "fixed.geojson") == 99


def test_source_override_actually_expires_the_file(tmp_path):
    """End-to-end: a short per-source TTL really deletes the dataset."""
    p = _downloaded(tmp_path, "osm.geojson", "connector/osm")
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=365,
                     ttl_by_source={"connector/osm": 2})
    cache.sync(today="2026-01-01")
    assert p.exists()
    summary = cache.sync(today="2026-01-10")  # past 2 days, far short of 365
    assert "osm.geojson" in summary["expired"]
    assert not p.exists()


# ── derived vs pinned ────────────────────────────────────────────────────────


def test_config_change_reaches_existing_rows(tmp_path):
    """A derived TTL is re-read every sync — otherwise config would be inert."""
    write_point(tmp_path / "a.geojson", 7.0, 50.0, "EPSG:25832")
    GeoCache(workspace=str(tmp_path), default_ttl_days=30).sync(today="2026-01-01")
    later = GeoCache(workspace=str(tmp_path), default_ttl_days=5)
    assert _ttl_of(later, "a.geojson") == 5


def test_pinned_ttl_survives_config_change(tmp_path):
    write_point(tmp_path / "keep.geojson", 7.0, 50.0, "EPSG:25832")
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=30)
    cache.sync(today="2026-01-01")
    assert cache.note("keep.geojson", "important", ttl_days=365)["ttl_pinned"] is True
    later = GeoCache(workspace=str(tmp_path), default_ttl_days=5,
                     ttl_by_source={"chester": 1})
    row = next(r for r in later.list(today="2026-01-02") if r["dataset"] == "keep.geojson")
    assert row["ttl_days"] == 365 and row["ttl_pinned"] is True


def test_pin_is_marked_in_the_inventory_file(tmp_path):
    write_point(tmp_path / "p.geojson", 7.0, 50.0, "EPSG:25832")
    cache = GeoCache(workspace=str(tmp_path))
    cache.sync()
    cache.note("p.geojson", "pinned", ttl_days=90)
    text = cache.inventory_path.read_text()
    assert "| 90* |" in text  # human-readable marker, survives a round-trip
    cache.sync()
    assert "| 90* |" in cache.inventory_path.read_text()


def test_legacy_unmarked_ttl_is_treated_as_derived(tmp_path):
    """Inventories written before pins existed must not freeze their TTL."""
    write_point(tmp_path / "old.geojson", 7.0, 50.0, "EPSG:25832")
    cache = GeoCache(workspace=str(tmp_path), default_ttl_days=30)
    cache.sync(today="2026-01-01")
    assert "| 30 |" in cache.inventory_path.read_text()  # no marker: derived
    assert _ttl_of(GeoCache(workspace=str(tmp_path), default_ttl_days=8), "old.geojson") == 8


# ── background sync ──────────────────────────────────────────────────────────


def test_periodic_sync_runs_and_stops(tmp_path):
    write_point(tmp_path / "a.geojson", 7.0, 50.0, "EPSG:25832")
    cache = GeoCache(workspace=str(tmp_path))
    fired = threading.Event()
    # Fire once immediately, then not again within the test's lifetime.
    stop = start_periodic_sync(cache, 1, on_result=lambda s: fired.set(),
                               initial_delay_hours=0)
    try:
        assert fired.wait(timeout=10), "background sync did not run"
        assert cache.inventory_path.exists()
    finally:
        stop.set()


def test_periodic_sync_disabled_returns_set_event(tmp_path):
    cache = GeoCache(workspace=str(tmp_path))
    stop = start_periodic_sync(cache, 0)
    assert stop.is_set()  # callers need no special case for "disabled"
    assert not cache.inventory_path.exists()  # nothing ran


def test_periodic_sync_survives_a_failing_scan(tmp_path):
    """A broken sync must log and keep looping, not kill the thread."""
    calls: list[int] = []

    class Boom(GeoCache):
        def sync(self, today=None):
            calls.append(1)
            raise RuntimeError("disk on fire")

    stop = start_periodic_sync(Boom(workspace=str(tmp_path)), 1, initial_delay_hours=0)
    try:
        for _ in range(100):  # ~1s max
            if calls:
                break
            threading.Event().wait(0.01)
        assert calls, "sync was never attempted"
    finally:
        stop.set()


def test_default_ttl_constant_is_the_fallback(tmp_path):
    write_point(tmp_path / "d.geojson", 7.0, 50.0, "EPSG:25832")
    assert _ttl_of(GeoCache(workspace=str(tmp_path)), "d.geojson") == DEFAULT_TTL_DAYS
