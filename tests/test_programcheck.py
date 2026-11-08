"""chester-code's static program check (Phase SP): each rule fires when sure, else stays quiet.

Test-Level 1 — no model. Every rule has programs it must catch **and** programs it must
let through: the frame example and every handbook template are correct programs, and a
rule that flags one of them would block what the coder is taught to write.
"""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.code_mode import CodeMode

from chester import toolreturns
from chester.code import coder, handbooks, programcheck


def _rules(code: str) -> list[str]:
    return [f.rule for f in programcheck.check(code)]


OSM = ('s = await osm_features(place="Regensburg", tags={"amenity": "school"},'
       ' output_path="s.gpkg")\n')


def test_a_buffer_on_degrees_is_caught():
    assert _rules(OSM + 'b = await vector_buffer(input_path=s["output"], distance=500,'
                  ' output_path="b.gpkg")') == ["metric-after-reprojection"]


def test_a_buffer_after_reprojection_passes():
    assert _rules(OSM + 'm = await vector_reproject(input_path=s["output"], output_path="m.gpkg",'
                  ' target_crs="EPSG:25832")\n'
                  'b = await vector_buffer(input_path=m["output"], distance=500,'
                  ' output_path="b.gpkg")') == []


def test_the_reprojection_must_be_used_not_just_present():
    """The trap that is easy to miss by eye: reprojected, then the original buffered."""
    assert _rules(OSM + 'm = await vector_reproject(input_path=s["output"], output_path="m.gpkg",'
                  ' target_crs="EPSG:25832")\n'
                  'b = await vector_buffer(input_path=s["output"], distance=500,'
                  ' output_path="b.gpkg")') == ["metric-after-reprojection"]


def test_the_crs_is_followed_through_clip_and_filter():
    program = (OSM + 'c = await vector_clip(input_path=s["output"], overlay_path="city.gpkg",'
               ' output_path="c.gpkg")\n'
               'f = await vector_filter(path=c["output"], expression="x > 1",'
               ' output_path="f.gpkg")\n'
               'b = await vector_buffer(input_path=f["output"], distance=500,'
               ' output_path="b.gpkg")')
    assert _rules(program) == ["metric-after-reprojection"]


def test_an_unknown_origin_gives_no_verdict():
    assert _rules('b = await vector_buffer(input_path="schools.gpkg", distance=500,'
                  ' output_path="b.gpkg")') == []
    assert _rules('x = await geo_python_run(code="...")\n'
                  'b = await vector_buffer(input_path=x["output"], distance=5,'
                  ' output_path="b.gpkg")') == []


def test_a_program_inside_main_is_read_too():
    body = "".join("    " + line + "\n" for line in (
        OSM + 'b = await vector_buffer(input_path=s["output"], distance=500,'
        ' output_path="b.gpkg")').splitlines())
    assert _rules("async def main():\n" + body + "await main()\n") == [
        "metric-after-reprojection"]


SPLIT = OSM + 'parts = await vector_split_by_geometry(path=s["output"], output_prefix="s")\n'


def test_keeping_only_points_after_a_split_is_caught():
    assert _rules(SPLIT + 'keep = [p["output"] for p in parts["parts"]'
                  ' if p["geometry_type"] == "Point"]') == ["polygons-kept"]
    assert _rules(SPLIT + 'keep = [p["output"] for p in parts["parts"]'
                  ' if p["geometry_type"] in ("Point", "MultiPoint")]') == ["polygons-kept"]


def test_keeping_points_and_polygons_passes():
    assert _rules(SPLIT + 'keep = [p["output"] for p in parts["parts"] if p["geometry_type"]'
                  ' in ("Point", "Polygon", "MultiPolygon")]') == []
    assert _rules(SPLIT + 'keep = [p["output"] for p in parts["parts"]]') == []


def test_what_the_coder_is_taught_passes_every_rule():
    example = coder._INSTRUCTIONS.split("```python\n")[1].split("```")[0]
    assert programcheck.check(example) == []
    for h in handbooks.HANDBOOKS:
        assert programcheck.check(h.template) == [], h.id


def test_the_geographic_tools_say_so_in_their_result_type():
    """`GEOGRAPHIC_OUTPUT` is not a second truth: the typed tools state EPSG:4326 themselves."""
    for name in programcheck.GEOGRAPHIC_OUTPUT & set(toolreturns.RETURNS):
        kind = toolreturns.RETURNS[name]
        assert "4326" in (kind.__doc__ or "") + str(kind.__annotations__) + _field_docs(kind), name


def _field_docs(kind) -> str:
    import inspect

    try:
        return inspect.getsource(kind)
    except OSError:
        return ""


def _run(programs: list[str], prompt: str = "go") -> list:
    seen: list = []
    turns = iter([*(ModelResponse(parts=[ToolCallPart("run_code", {"code": c})])
                    for c in programs), ModelResponse(parts=[TextPart("done")])])

    def model(messages, _info):
        seen.extend((p.part_kind, str(p.content)) for p in messages[-1].parts
                    if p.part_kind in ("tool-return", "retry-prompt"))
        return next(turns)

    agent = Agent(FunctionModel(model), capabilities=[
        CodeMode(tools=["osm_features", "vector_buffer"]), programcheck.ProgramCheckCapability()])

    @agent.tool_plain
    def osm_features(place: str, tags: dict, output_path: str) -> dict:
        """Stub."""
        return {"ok": True, "output": "/ws/s.gpkg"}

    @agent.tool_plain
    def vector_buffer(input_path: str, distance: float, output_path: str) -> dict:
        """Stub."""
        return {"ok": True, "output": "/ws/b.gpkg"}

    agent.run_sync(prompt)
    return seen


BAD = OSM + 'b = await vector_buffer(input_path=s["output"], distance=500, output_path="b.gpkg")'


def test_a_flagged_program_does_not_run_and_the_model_hears_why():
    seen = _run([BAD, BAD])
    assert seen[0][0] == "retry-prompt" and "line 2" in seen[0][1]
    assert "vector_reproject" in seen[0][1]


def test_the_same_finding_twice_lets_the_program_run():
    """The check may be wrong; a false alarm costs one round, not the run."""
    seen = _run([BAD, BAD])
    assert seen[1][0] == "tool-return"


def test_the_check_can_be_switched_off_for_a_comparison_run(monkeypatch):
    """`CHESTER_PROGRAMCHECK=0` drops it — the A/B arm of SP.3; default is on."""
    def has_check():
        return any(isinstance(c, programcheck.ProgramCheckCapability)
                   for c in coder.code_capabilities(planning=False))
    assert has_check()
    monkeypatch.setenv("CHESTER_PROGRAMCHECK", "0")
    assert not has_check()


GEO = 'place = await geocode(query="Regensburg")\n'


def test_a_geocoded_bbox_without_any_clip_is_caught():
    assert _rules(GEO + 's = await osm_features(bbox=place["bbox"], tags={"amenity": "school"},'
                  ' output_path="s.gpkg")') == ["named-place-clipped"]
    # the alias qwen wrote on 2026-11-05
    assert _rules(GEO + 'bbox = place["bbox"]\n'
                  's = await osm_features(bbox=bbox, tags={"amenity": "school"},'
                  ' output_path="s.gpkg")') == ["named-place-clipped"]


def test_a_place_a_clip_or_a_literal_window_passes():
    assert _rules(OSM) == []
    assert _rules(GEO + 's = await osm_features(bbox=place["bbox"], tags={"a": "b"},'
                  ' output_path="s.gpkg")\n'
                  'c = await vector_clip(input_path=s["output"], overlay_path="city.gpkg",'
                  ' output_path="c.gpkg")') == []
    assert _rules('s = await osm_features(bbox=[12.0, 49.0, 12.1, 49.1], tags={"a": "b"},'
                  ' output_path="s.gpkg")') == []


def test_the_city_districts_wfs_by_bbox_is_not_flagged():
    """The districts come from the city's own WFS by bbox — correct, and must stay quiet."""
    assert _rules(GEO + 'd = await wfs_features(url="u", typename="t", bbox=place["bbox"],'
                  ' output_path="d.gpkg")') == []


BUFFER = (OSM + 'm = await vector_reproject(input_path=s["output"], output_path="m.gpkg",'
          ' target_crs="EPSG:25832")\n'
          'b = await vector_buffer(input_path=m["output"], distance=800, output_path="b.gpkg")')


def test_a_buffer_for_a_travel_time_task_is_caught():
    for task in ("Supermärkte in 10 Gehminuten Entfernung", "Was ist zu Fuß erreichbar?",
                 "stops within a 15 min walk", "Einzugsgebiet: 5-Minuten-Fahrzeit"):
        found = [f.rule for f in programcheck.check(BUFFER, task)]
        assert found == ["travel-time-not-buffer"], task


def test_a_buffer_task_or_a_service_area_passes():
    assert programcheck.check(BUFFER, "500-Meter-Einzugszone um alle Schulen") == []
    assert programcheck.check(BUFFER + '\nn = await service_area(network_path="n.gpkg",'
                              ' output_path="sa.gpkg", start_lon=12.1, start_lat=49.0,'
                              ' minutes=10)', "10 Gehminuten") == []
    assert programcheck.check(BUFFER) == []  # no task text, no verdict


def test_the_guard_reads_the_users_task():
    """The travel-time rule needs the prompt — the capability must hand it over."""
    program = OSM + ('b = await vector_buffer(input_path="x.gpkg", distance=800,'
                     ' output_path="b.gpkg")')
    seen = _run([program, program], prompt="Welche Schulen sind in 10 Gehminuten erreichbar?")
    assert seen[0][0] == "retry-prompt" and "service_area" in seen[0][1]
