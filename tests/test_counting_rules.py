"""Two rules every cell broke on the held-out cases — held where the model reads them.

Measured 2026-09-30 to 2026-10-04 over `heldout-regensburg-*`:

* `danube-bridges`: every gemma4 run, single agent and team, counted OSM *ways* and
  reported 43, 44, 56 or 65 bridges; only the hosted model grouped them into ten
  named bridges. The rule goes into the `osm_features` description, which every
  adapter shows and which reaches the data ressort with the tool itself.
* `tallest-buildings-map`: the hosted model found the right ten buildings with
  addresses and then mapped all 2 492 buildings of the Altstadt coloured by height.
  The rule goes into the map instructions, which the single agent and the output
  ressort both read.

Text, not code: whether a model follows it is for Test-Level 3 to show. What this
holds is that the sentence is there, in the place the model actually looks.
"""

from __future__ import annotations

from chester import maptools, osmtools
from chester.team import ressorts


def _osm_features_doc() -> str:
    tool = next(t for t in osmtools.build_tools("/tmp/ws") if t.__name__ == "osm_features")
    return tool.__doc__ or ""


def test_osm_features_says_a_feature_is_not_an_object():
    doc = _osm_features_doc()
    assert "not a real-world object" in doc
    assert "group by" in doc and "bridge:name" in doc, "the way out must be named"


def test_the_map_instructions_ask_for_the_selection_itself():
    text = maptools.instructions()
    assert "A selection is drawn as a selection" in text
    assert "own layer" in text


def test_both_rules_reach_the_ressort_that_needs_them():
    assert "A selection is drawn as a selection" in ressorts.ressort_instructions("output")
    data_tools = ressorts.ressort_tools("data", "/tmp/ws", {
        "roots": [], "postgis": None, "stac_catalogs": None, "ttl_by_source": {}})
    osm = next(t for t in data_tools if t.__name__ == "osm_features")
    assert "not a real-world object" in (osm.__doc__ or "")
