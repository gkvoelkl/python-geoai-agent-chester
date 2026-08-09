"""Tests for the LoD2 building-height connector (chester/lod2.py + capability).

The CityGML parser and tile derivation are covered offline (a synthetic tile,
pure coordinate math); the actual state downloads are one opt-in network test.
"""

from __future__ import annotations

import pytest

from chester import lod2

# A minimal AdV-style LoD2 building: 10×10 m square footprint at 100 m elevation,
# measuredHeight 12.5 m, on Teststraße 7. Namespaces differ from the real tiles on
# purpose — the parser matches local tag names, so it must still read it.
_CITYGML = """<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0"
    xmlns:bldg="http://www.opengis.net/citygml/building/2.0"
    xmlns:gml="http://www.opengis.net/gml"
    xmlns:xAL="urn:oasis:names:tc:ciq:xsdschema:xAL:2.0">
  <core:cityObjectMember>
    <bldg:Building gml:id="B1">
      <bldg:measuredHeight uom="urn:adv:uom:m">12.5</bldg:measuredHeight>
      <bldg:address><core:Address><core:xalAddress><xAL:AddressDetails>
        <xAL:Thoroughfare><xAL:ThoroughfareNumber>7</xAL:ThoroughfareNumber>
        <xAL:ThoroughfareName>Teststraße</xAL:ThoroughfareName></xAL:Thoroughfare>
      </xAL:AddressDetails></core:xalAddress></core:Address></bldg:address>
      <bldg:boundedBy><bldg:GroundSurface><bldg:lod2MultiSurface>
        <gml:MultiSurface><gml:surfaceMember><gml:Polygon><gml:exterior>
        <gml:LinearRing><gml:posList>0 0 100 10 0 100 10 10 100 0 10 100 0 0 100</gml:posList>
        </gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember></gml:MultiSurface>
      </bldg:lod2MultiSurface></bldg:GroundSurface></bldg:boundedBy>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
"""


def test_parse_citygml_extracts_height_address_and_footprint(tmp_path):
    p = tmp_path / "tile.gml"
    p.write_text(_CITYGML, encoding="utf-8")
    gdf = lod2.parse_citygml(str(p), epsg=25832)
    assert len(gdf) == 1
    row = gdf.iloc[0]
    assert row["measured_height"] == 12.5
    assert row["street"] == "Teststraße"
    assert row["housenumber"] == "7"
    assert row["gml_id"] == "B1"
    # 10×10 m square footprint, in the metric CRS, so area ≈ 100 m².
    assert gdf.crs.to_epsg() == 25832
    assert abs(row.geometry.area - 100.0) < 1e-6
    assert row.geometry.geom_type in ("Polygon", "MultiPolygon")


def test_parse_citygml_empty_tile_returns_empty_frame(tmp_path):
    p = tmp_path / "empty.gml"
    p.write_text('<core:CityModel xmlns:core="x"/>', encoding="utf-8")
    gdf = lod2.parse_citygml(str(p))
    assert gdf.empty and "measured_height" in gdf.columns


def test_bayern_tiles_cover_regensburg_maximilianstrasse():
    # Maximilianstraße bbox → the two 2 km Bayern tiles that actually hold it.
    tiles = lod2._bayern_tiles([12.0955, 49.0135, 12.1015, 49.0195])
    names = {n for _u, n in tiles}
    assert "726_5432.gml" in names
    assert all(u.endswith(n) for u, n in tiles)


def test_nrw_tiles_cover_koeln():
    tiles = lod2._nrw_tiles([6.955, 50.940, 6.965, 50.945])
    names = {n for _u, n in tiles}
    assert "LoD2_32_356_5645_1_NW.gml" in names
    assert all("opengeodata.nrw.de" in u for u, _n in tiles)


def test_brandenburg_tiles_are_zipped_citygml_utm33():
    # Potsdam window → zipped CityGML tiles on the Brandenburg server (UTM33).
    tiles = lod2._brandenburg_tiles([13.055, 52.393, 13.070, 52.402])
    names = {n for _u, n in tiles}
    assert tiles and all(n.startswith("lod2_33") and n.endswith(".zip")
                         for n in names)
    assert all("geobasis-bb.de" in u for u, _n in tiles)


def test_mv_tiles_are_zipped_citygml_utm33_via_atom_endpoint():
    # Schwerin window → 2 km CityGML zips via the MV Atom download endpoint (UTM33).
    tiles = lod2._mv_tiles([11.408, 53.625, 11.422, 53.635])
    assert tiles and all(n.startswith("lod2_33_") and n.endswith("_2_gml.zip")
                         for _u, n in tiles)
    assert all("geodaten-mv.de" in u and "dataset=" in u for u, _n in tiles)


def test_registry_has_open_and_documented_states():
    codes = set(lod2.BUNDESLAENDER)
    # wired-and-verified
    assert {"BY", "NW", "BB", "MV"} <= {s.code for s in lod2.open_states()}
    # a broad open-data coverage is advertised, but BW (fee-based) is excluded
    assert "BW" not in codes
    assert len(codes) >= 15


def test_fetch_lod2_unknown_state_is_reported(tmp_path):
    r = lod2.fetch_lod2([12.0, 49.0, 12.1, 49.1], str(tmp_path / "o.gpkg"),
                        str(tmp_path / "cache"), state="ZZ")
    assert r["ok"] is False and "unknown Bundesland" in r["error"]


def test_fetch_lod2_documented_only_state_declines(tmp_path):
    # A state that is open but not wired must decline clearly (not fake a fetch).
    r = lod2.fetch_lod2([9.9, 53.5, 10.0, 53.6], str(tmp_path / "o.gpkg"),
                        str(tmp_path / "cache"), state="HH")
    assert r["ok"] is False and "not yet wired" in r["error"]


def test_lod2_sources_tool_lists_wired_states():
    from chester.capabilities.lod2 import GeoLod2Capability

    tools = {n: getattr(t, "function", t)
             for n, t in GeoLod2Capability(workspace=".").get_toolset().tools.items()}
    out = tools["lod2_sources"]()
    assert out["ok"] and set(out["wired"]) >= {"BY", "NW"}


@pytest.mark.network
def test_fetch_lod2_mv_end_to_end(tmp_path):
    # Schwerin → MV via the Atom download endpoint (UTM33, zipped CityGML).
    r = lod2.fetch_lod2([11.408, 53.625, 11.422, 53.635],
                        str(tmp_path / "sn.gpkg"), str(tmp_path / "cache"))
    assert r["ok"], r
    assert r["state"] == "MV" and r["crs"] == "EPSG:25833"
    assert r["buildings"] > 100 and r["with_height"] == r["buildings"]


@pytest.mark.network
def test_fetch_lod2_bayern_end_to_end(tmp_path):
    # Real fetch: Maximilianstraße, Regensburg → measured heights per building.
    out = tmp_path / "max.gpkg"
    r = lod2.fetch_lod2([12.0955, 49.0135, 12.1015, 49.0195], str(out),
                        str(tmp_path / "cache"), street="Maximilianstraße")
    assert r["ok"], r
    assert r["state"] == "BY"
    assert r["buildings"] >= 20
    assert r["with_height"] == r["buildings"]
    assert 3 < r["height_stats_m"]["max"] < 60
    assert out.exists()
