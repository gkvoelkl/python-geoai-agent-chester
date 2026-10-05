"""Test-Level 2 for Chester-MCP — the catalogue a foreign client sees.

What is checked is the **catalogue and the contract**, not the tools themselves: those
live in the wrapper layer and have their own checks there. This is about what can only
go wrong in delivery — a missing tool, one delivered too many, a description that
arrives empty at the client.

No network, no model, no running server: `collect_tools` is a pure function.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chester import mcpserver

WS = "/tmp/chester-mcp-test"


def _names() -> set[str]:
    return {t.__name__ for t in mcpserver.collect_tools(WS)}


def test_the_catalogue_carries_the_whole_wrapper_layer():
    """Every wrapper module comes along — without curation.

    Decided 2026-09-13: deliver all. Whether a foreign model still picks the right tool
    at this breadth is itself a finding of cell F+MCP and must not be cleared away
    beforehand by a narrower selection.
    """
    packages = Path(mcpserver.__file__).parents[2]
    modules = sorted(p.stem for p in packages.glob("*/chester/*tools.py"))
    assert mcpserver.wrapper_modules() == modules, "ein Hüllenmodul fehlt im Katalog"
    assert len(_names()) >= 80, "der Katalog ist unerwartet klein"


def test_the_escape_hatches_stay_out_of_the_catalogue():
    """No `geo_python_run`, no `qgis_python`, no `inspect_map`.

    Decided 2026-09-13: the escape hatch stays with Chester's own agent. That keeps the
    server free of remote execution — and the question does not come up anew later. They
    live outside the wrapper layer; this test pins that they do not wander in one day.
    """
    verboten = {"geo_python_run", "qgis_python", "inspect_map"} & _names()
    assert not verboten, f"Fernausführung im MCP-Katalog: {sorted(verboten)}"


def test_every_tool_arrives_with_a_description():
    """For an MCP client the docstring is the only text channel to the model.

    Measured 2026-09-13: server `instructions` do not reach the user through the
    `remote-devices` bridge, tool descriptions do, verbatim. A tool without a docstring
    is mute there.
    """
    ohne = [t.__name__ for t in mcpserver.collect_tools(WS)
            if not (t.__doc__ or "").strip()]
    assert not ohne, f"stumme Werkzeuge: {ohne}"


def test_the_gate_is_offered_and_says_it_does_not_enforce():
    """`validate_result` is there — and says itself that it enforces nothing.

    That is what the cell measures, not a shortcoming of the implementation: over MCP,
    "Correctness is a loop phase" becomes a tool that *can* be called. Were the return
    value mute about it, a gate never called would read like a passed one.
    """
    tools = {t.__name__: t for t in mcpserver.collect_tools(WS)}
    assert "validate_result" in tools
    result = tools["validate_result"](["gibtsnicht.gpkg"])
    assert result["enforced"] is False
    assert result["must_fix"] is True          # a missing file is a finding
    assert result["findings"][0]["check"] == "exists"


def test_a_wrapper_module_without_build_tools_is_an_error(monkeypatch):
    """Silently delivering a smaller catalogue is the real risk.

    That is exactly what happened while `vectoroptools` was called `op_tools`: no error,
    just ten missing vector operations (2026-09-14). So the server aborts instead of
    skipping.
    """
    from chester import wrapperlayer  # the rule lives there since 2026-09-19

    monkeypatch.setattr(wrapperlayer, "wrapper_modules", lambda exclude=None: ["workspace"])
    with pytest.raises(RuntimeError, match="build_tools"):
        mcpserver.collect_tools(WS)


def test_the_server_really_speaks_the_protocol(tmp_path):
    """As a **separate process** over stdio — anything else only checks the catalogue.

    The tests above call `collect_tools` in the same process; they would stay green even
    if the server did not start at all, or if `FastMCP` refused one of the tools. Here it
    runs the way it runs for the user: its own process, handshake, catalogue query, a
    real call.

    **What it does not cover, cross-checked on 2026-09-14:** a stray line on stdout.
    Built in and expected the test to fail — it stayed green. Neither `fastmcp` nor the
    `mcp` package redirect stdout; the line went out before the handshake and the client
    silently skipped it. A line *during* the session is not cleared by this — it is
    merely unchecked.
    """
    import asyncio
    import os
    import sys

    fastmcp = pytest.importorskip("fastmcp")
    from fastmcp.client.transports import StdioTransport

    async def frage() -> tuple[int, dict]:
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "chester.mcpserver"],
            env={**os.environ, "CHESTER_WORKSPACE": str(tmp_path)},
        )
        async with fastmcp.Client(transport) as client:
            tools = await client.list_tools()
            antwort = await client.call_tool("validate_result", {"paths": ["fehlt.gpkg"]})
            return len(tools), antwort.data

    anzahl, befund = asyncio.run(frage())
    assert anzahl >= 80, "der Server meldet einen zu kleinen Katalog an"
    assert befund["must_fix"] is True and befund["enforced"] is False


def test_the_workspace_does_not_depend_on_the_working_directory(monkeypatch, tmp_path):
    """Where the server writes must not depend on how it was started.

    `DEFAULT_WORKSPACE` is relative (`.chester/workspace`). Chester's agent runs from the
    project directory, an MCP server does not: Claude Desktop starts it with a working
    directory nobody chose. Measured 2026-09-14 with ``cwd="/"`` the server died on
    `'.chester/workspace'`.

    And it cannot ask either: **the client provides no workspace.** MCP knows `roots`,
    but SEP-2577 removed server-initiated requests from the protocol. The directory is
    decided at start or not at all.
    """
    import os

    monkeypatch.chdir(tmp_path)
    ohne_env = mcpserver.resolve_workspace({})
    assert os.path.isabs(ohne_env)
    assert str(tmp_path) not in ohne_env, "der Workspace folgt dem Arbeitsverzeichnis"
    root = Path(__file__).resolve().parent.parent
    assert ohne_env == str(root / ".chester" / "workspace"), "not the agent's shared cache"

    ziel = tmp_path / "eigener"
    assert mcpserver.resolve_workspace({"CHESTER_WORKSPACE": str(ziel)}) == str(ziel)


def test_read_artifact_cannot_leave_the_cache(tmp_path):
    """A tool that returns bytes is a read primitive — and must be fenced in.

    `chester.workspace.resolve_path` passes absolute paths through on **read** on
    purpose: reading user data in place is a feature, and harmless for Chester's own
    agent, because no other tool hands out file contents. That does not hold for
    `read_artifact` — so it resolves by itself and reduces to the file name.
    """
    from chester.artifacttools import build_tools

    (tmp_path / "geocache").mkdir()
    geheim = tmp_path.parent / "geheim.txt"
    geheim.write_text("nicht für fremde Augen")

    lies = build_tools(str(tmp_path))[0]
    for roh in (str(geheim), "/etc/passwd", "../geheim.txt", "~/.ssh/id_rsa"):
        r = lies(roh)
        assert r["ok"] is False, f"{roh!r} kam durch"
        assert "nicht für fremde Augen" not in str(r)


def test_read_artifact_gives_pictures_and_refuses_data(tmp_path):
    """Image as image, text as text, geodata with a reasoned refusal.

    The refusals name the better route instead of just saying no: an HTML map is a web
    page with embedded data — as text it says nothing about the picture — and a
    GeoPackage is not something to look at but a dataset for `vector_info`.
    """
    from chester.artifacttools import build_tools

    cache = tmp_path / "geocache"
    cache.mkdir()
    (cache / "k.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (cache / "k.html").write_text("<html>viel eingebettetes GeoJSON</html>")
    (cache / "d.gpkg").write_bytes(b"SQLite format 3\x00")
    (cache / "t.csv").write_text("a,b\n1,2\n")

    lies = build_tools(str(tmp_path))[0]

    bild = lies("k.png")
    assert bild["ok"] and bild["media_type"] == "image/png" and bild["content_base64"]

    text = lies("t.csv")
    assert text["ok"] and text["text"].startswith("a,b") and text["truncated"] is False

    html = lies("k.html")
    assert html["ok"] is False and html["look_at_instead"].endswith("k.png")

    daten = lies("d.gpkg")
    assert daten["ok"] is False and "vector_info" in daten["use_instead"]


def test_the_picture_is_attached_only_on_request_by_default():
    """Default off — otherwise F+MCP would get a look that F+ does not have.

    In F+ Chester's agent does not see its map by itself: the gate's visual check runs
    only from level 2, the default is 1, and `inspect_map` has to be called. An
    automatically attached image would thus be no level playing field but a head start —
    and one the evaluation would know nothing about. On request (`read_artifact`,
    recognisable by `content_base64`) the image **always** travels.
    """
    assert mcpserver.attach_pictures({}) is False
    assert mcpserver.attach_pictures({mcpserver.ATTACH_ENV: "1"}) is True

    def mit_pfad() -> dict:
        """A map with a still-image path."""
        return {"ok": True, "picture": "/gibt/es/nicht.png"}

    def auf_anfrage() -> dict:
        """Ein ausdrücklich angefordertes Bild."""
        return {"ok": True, "media_type": "image/png", "content_base64": "AAAA"}

    assert mcpserver._with_picture(mit_pfad, automatic=False)() == {
        "ok": True, "picture": "/gibt/es/nicht.png"}

    geliefert = mcpserver._with_picture(auf_anfrage, automatic=False)()
    assert [type(b).__name__ for b in geliefert.content] == ["ImageContent"]
    # The base64 lump travels in the image block, not additionally in the structure.
    assert "content_base64" not in geliefert.structured_content


def test_the_server_records_which_tools_were_called(tmp_path):
    """Without its own log the F+MCP cell cannot be evaluated.

    Claude Desktop's MCP log notes `method="tools/call"` and leaves out the parameters —
    never the tool *name* (checked 2026-09-14). From outside only the number of calls is
    visible. For L+ and F+ the bench records tool count, distinct tools and coverage;
    without this file F+MCP would have none of it — and the cell's core question, whether
    the model calls the voluntary `validate_result`, would stay unanswerable for good.

    What is recorded is **what** and **how it ended**, not the payload: arguments can
    carry base64 images or whole geometries.
    """
    def geht_gut() -> dict:
        """Ein Werkzeug."""
        return {"ok": True, "features": 3}

    def geht_schief() -> dict:
        """Noch eins."""
        return {"ok": False, "error": "nein"}

    for fn in (geht_gut, geht_schief, geht_gut):
        mcpserver._with_picture(fn, automatic=False, workspace=str(tmp_path))()

    zeilen = mcpserver.read_call_log(str(tmp_path))
    assert [z["tool"] for z in zeilen] == ["geht_gut", "geht_schief", "geht_gut"]
    assert [z["ok"] for z in zeilen] == [True, False, True]
    assert all("duration_s" in z and "ts" in z for z in zeilen)
    # No payload in the log.
    assert all(set(z) <= {"ts", "tool", "duration_s", "ok"} for z in zeilen)

    # Without a workspace nothing is logged (calls in tests, adapters without a target).
    assert mcpserver.read_call_log(str(tmp_path / "leer")) == []
