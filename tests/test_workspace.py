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
