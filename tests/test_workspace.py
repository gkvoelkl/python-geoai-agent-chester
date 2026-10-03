"""Unit tests for workspace path resolution (pure, no external deps)."""

from pathlib import Path

from chester.workspace import resolve_path


def test_sloppy_paths_collapse_to_workspace(tmp_path):
    ws = str(tmp_path / "ws")
    # Relative paths now collapse into the GeoCache working dir (geocache/),
    # including a redundant leading "geocache/" the model may add itself.
    expected = str(Path(ws) / "geocache" / "foo.tif")
    for variant in (
        "foo.tif",
        "workspace/foo.tif",
        "chester/workspace/foo.tif",  # the dropped-dot bug
        ".chester/workspace/foo.tif",
        ".selmakit/workspace/foo.tif",  # legacy name still collapses
        "geocache/foo.tif",
        ".chester/workspace/geocache/foo.tif",  # no geocache/geocache nesting
    ):
        assert resolve_path(variant, ws) == expected


def test_absolute_path_passthrough(tmp_path):
    p = str(tmp_path / "x.tif")
    assert resolve_path(p, str(tmp_path / "ws")) == p


def test_existing_relative_file_passthrough(tmp_path, monkeypatch):
    f = tmp_path / "exists.tif"
    f.write_text("x")
    monkeypatch.chdir(tmp_path)
    assert resolve_path("exists.tif", str(tmp_path / "ws")) == "exists.tif"


def test_parent_directory_is_created(tmp_path):
    out = resolve_path("sub/deep/foo.tif", str(tmp_path / "ws"))
    assert Path(out).parent.is_dir()


def test_dot_slash_prefix_collapses(tmp_path):
    # The model writes "./workspace/x"; the leading "./" must not defeat the
    # workspace alias and nest a spurious geocache/workspace/ dir.
    ws = str(tmp_path / "ws")
    expected = str(Path(ws) / "geocache" / "foo.tif")
    for variant in ("./workspace/foo.tif", "./foo.tif", "./geocache/foo.tif"):
        assert resolve_path(variant, ws) == expected


def test_leading_slash_workspace_prefix_collapses(tmp_path):
    # An absolute-looking "/workspace/x" is the model spelling the workspace,
    # not a real root path — it collapses into the geocache too.
    ws = str(tmp_path / "ws")
    expected = str(Path(ws) / "geocache" / "foo.tif")
    for variant in (
        "/workspace/foo.tif",
        "/geocache/foo.tif",
        "/.chester/workspace/foo.tif",
    ):
        assert resolve_path(variant, ws) == expected


def test_genuine_absolute_non_workspace_path_passes_through(tmp_path):
    # A real absolute path to user data matches no workspace prefix → untouched.
    p = "/Users/someone/data/echt.gpkg"
    assert resolve_path(p, str(tmp_path / "ws")) == p


def test_bare_root_level_slash_collapses_to_workspace(tmp_path):
    # The model writes "/buildings.geojson" (leading slash, no workspace prefix);
    # writing to the filesystem root fails read-only, so treat it as workspace.
    ws = str(tmp_path / "ws")
    out = resolve_path("/buildings.geojson", ws)
    assert out == str(Path(ws) / "geocache" / "buildings.geojson")


# ── write=True: Ausgaben werden eingesperrt, Eingaben nicht ──────────────────
# Gefunden am 2026-09-13 (F+, `heldout-regensburg-danube-bridges`): `render_map` bekam
# `/tmp/…_v2.html` und schrieb dorthin — ohne Inventareintrag, ohne TTL, und das Gate,
# das Datensätze prüft, die der Lauf erzeugt *und* die Antwort erwähnt, sah die
# geschnittenen Ebenen nicht mehr und meldete fälschlich „extent unresolved".


def test_an_absolute_output_is_confined_to_the_cache(tmp_path):
    ws = str(tmp_path / "ws")
    out = resolve_path("/tmp/karte.html", ws, write=True)
    assert out == str(Path(ws) / "geocache" / "karte.html")


def test_a_nested_absolute_output_keeps_only_its_basename(tmp_path):
    """Ein absoluter Pfad darf seinen Baum nicht im Cache nachbauen."""
    ws = str(tmp_path / "ws")
    out = resolve_path("/Users/someone/tief/verschachtelt/x.gpkg", ws, write=True)
    assert out == str(Path(ws) / "geocache" / "x.gpkg")


def test_an_existing_absolute_file_is_not_overwritten_in_place(tmp_path):
    """Quelldaten des Nutzers bleiben unangetastet — sonst überschriebe eine Ausgabe sie."""
    src = tmp_path / "quelle.gpkg"
    src.write_text("x")
    ws = str(tmp_path / "ws")
    out = resolve_path(str(src), ws, write=True)
    assert out == str(Path(ws) / "geocache" / "quelle.gpkg")
    assert src.read_text() == "x"


def test_reads_are_unchanged(tmp_path):
    """Ohne `write` bleibt alles wie bisher — Quelldaten werden am Ort gelesen."""
    p = str(tmp_path / "x.tif")
    assert resolve_path(p, str(tmp_path / "ws")) == p


def test_the_parent_directory_exists_after_a_write_resolve(tmp_path):
    ws = str(tmp_path / "ws")
    out = resolve_path("/tmp/tief.gpkg", ws, write=True)
    assert Path(out).parent.is_dir(), "Schreiben schlüge fehl, das Verzeichnis fehlt"


def test_a_write_can_never_leave_the_workspace(tmp_path):
    """Kein `output_path` bricht aus dem Cache aus — auch kein bösartiger.

    Bis zum 14.09.2026 tat `../../ausbruch.gpkg` genau das: Es wurde zu
    `<ws>/geocache/../../ausbruch.gpkg`, und die Datei landete **nachweislich**
    ausserhalb — mit `vector_reproject` gegengeprüft, nicht nur am Pfad. Absolute
    Pfade waren längst auf den Basisnamen reduziert, `..` war es nicht.

    Für Chesters eigenen Agenten wäre das unwahrscheinlich. Über den MCP-Server
    bestimmt ein **fremdes** Modell diesen Parameter, und der Server verspricht, dass
    Ausgaben im Cache landen — ein Versprechen, das nur für wohlmeinende Eingaben
    gilt, ist keines.
    """
    import os

    ws = str(tmp_path)
    for roh in ("../../ausbruch.gpkg", "../etc/passwd", "a/../../../b.gpkg",
                "/etc/passwd", "/Users/jemand/Desktop/x.gpkg", "~/Desktop/x.gpkg"):
        ziel = os.path.normpath(resolve_path(roh, ws, write=True))
        assert ziel.startswith(os.path.normpath(ws) + os.sep), (
            f"{roh!r} schreibt nach {ziel} — ausserhalb des Workspace"
        )

    # Lesen bleibt ausdrücklich durchlässig: Nutzerdaten werden am Ort gelesen.
    fremd = tmp_path.parent / "fremd.gpkg"
    fremd.write_text("x")
    assert resolve_path(str(fremd), ws) == str(fremd)


def test_the_models_own_spelling_of_the_cache_collapses(tmp_path):
    """`GeoCache/x.gpkg` is **one** directory, not two.

    Measured 2026-09-26 in the first Test-Level-2 run against the team
    (`intersection-not-selection`, `within-on-the-boundary`): the vector ressort called
    the right tools in the right order, on the right ressort, and got the right numbers
    — 3 of 4 points inside, the boundary point correctly excluded. Both results landed
    in `geocache/GeoCache/…`, one directory below the cache. The probe looked for the
    file at the canonical place, found nothing, and failed.

    The cause is not the model but this project's own spelling: the instructions call
    the cache **GeoCache** throughout (`inventorytools`, `vectortools`: "put outputs in
    the GeoCache"), while the comparison was against the lower-case directory. Every
    capitalisation therefore slipped through — `GeoCache/`, `Workspace/`,
    `.Chester/workspace/` each built a nested tree inside the cache. It is the most
    expensive kind of defect: everything else about the run was right, and the damage
    only shows in the *next* step, which looks for the layer under its plain name.
    """
    ws = str(tmp_path / "ws")
    expected = str(Path(ws) / "geocache" / "foo.tif")
    for variant in (
        "GeoCache/foo.tif",  # the spelling the instructions use
        "Geocache/foo.tif",
        "GEOCACHE/foo.tif",
        "Workspace/foo.tif",
        ".Chester/workspace/foo.tif",
        ".Chester/Workspace/GeoCache/foo.tif",
    ):
        assert resolve_path(variant, ws, write=True) == expected, variant
        assert resolve_path(variant, ws) == expected, variant
