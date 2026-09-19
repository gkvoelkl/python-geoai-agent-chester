"""`read_artifact` — hand out a produced artifact on request.

Phase KM. The tool exists because **for a foreign client a path is not a reference
but a string**. Measured 2026-09-14 (cell F+MCP, test case 1): the model produced a
correct map, got its path back, and Claude Desktop reported that files stored at
that location cannot be displayed — as a substitute it built a bar chart from the
numbers. The client may not read Chester's cache; its own output path lives in a VM
into which nothing can be mounted from outside. A shared file system does not exist
and cannot exist — the content has to travel through the protocol.

**Why a tool and not attached automatically.** In F+ Chester's agent does not see
its map on its own either: the gate's visual check only runs from strictness level
2, the default is 1, and it has to call `inspect_map`. A tool that *can* be called
reproduces the same situation. If the picture were attached to every return value,
cell F+MCP would get a free look that F+ does not have — and the measurement would
not know. (For product use there is the switch `CHESTER_MCP_ATTACH_PICTURES`, see
`chester/mcpserver.py`.)

**Confined, and that is half the construction.** A tool that returns file contents
is a read primitive. `chester.workspace.resolve_path` deliberately passes absolute
paths through on read — reading user data in place is a feature, and harmless for
Chester's own agent because no other tool returns bytes. Here it would not be:
`read_artifact("~/.ssh/id_rsa")` would hand a foreign model the key. So this module
resolves **by itself** and requires the result to lie under `<workspace>/geocache/`.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path

#: Image formats that travel through the protocol as an image.
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

#: Text formats that make sense as text — capped, see below.
TEXTS = {".csv": "text/csv", ".json": "application/json", ".txt": "text/plain",
         ".md": "text/markdown", ".geojson": "application/geo+json",
         ".wkt": "text/plain", ".prj": "text/plain"}

#: A picture beyond this is no longer a picture but an accident.
MAX_PICTURE_BYTES = 5 * 1024 * 1024

#: Text beyond this is truncated — with a note, never silently.
MAX_TEXT_BYTES = 100 * 1024


def build_tools(workspace: str) -> list[Callable[..., dict]]:
    """`read_artifact`, bound to ``workspace``."""
    ws = workspace

    def read_artifact(path: str) -> dict:
        """Give back the content of a file this server produced, so you can look at it.

        The paths other tools return are **strings, not references**: this server
        writes into its own cache, which your side cannot read. Call this to actually
        see what you made — most usefully on the ``picture`` that ``render_map``
        returns, which is a flat PNG of the same map.

        A PNG or JPEG comes back as an image. Small text files (CSV, JSON, GeoJSON,
        TXT) come back as text, truncated past 100 kB with a note saying so.

        Two things it refuses, each with the better route named: an HTML map (a web
        page with the data embedded — look at the ``.png`` beside it instead), and
        geodata such as GeoPackage, GeoTIFF or LAZ (use ``vector_info``,
        ``raster_info`` and the analysis tools; those files are data, not a view).

        Only files inside this server's own cache can be read. Anything else is
        refused — this is not a way to read the machine's filesystem.
        """
        cache = (Path(ws) / "geocache").resolve()
        target = (cache / Path(path).name).resolve()
        # Only the file name counts: a path from outside must not decide where
        # reading happens. The same reduction as on write.
        if not str(target).startswith(str(cache) + "/"):
            return {"ok": False, "error": "outside this server's cache"}
        if not target.is_file():
            neighbours = sorted(p.name for p in cache.glob("*") if p.is_file())[:12]
            return {"ok": False, "error": f"no artifact named '{target.name}'",
                    "available": neighbours}

        suffix = target.suffix.lower()
        size = target.stat().st_size

        if suffix in IMAGES:
            if size > MAX_PICTURE_BYTES:
                return {"ok": False, "error": f"the picture is {size // 1024} kB, "
                        f"past the {MAX_PICTURE_BYTES // 1024} kB limit"}
            return {"ok": True, "path": str(target), "bytes": size,
                    "media_type": IMAGES[suffix],
                    "content_base64": base64.b64encode(target.read_bytes()).decode()}

        if suffix == ".html":
            png = target.with_suffix(".png")
            return {"ok": False,
                    "error": "an HTML map is a web page with the data embedded — "
                             "reading it as text tells you nothing about the picture",
                    "look_at_instead": str(png) if png.is_file() else None,
                    "hint": "call read_artifact on the .png beside it"}

        if suffix in TEXTS:
            raw = target.read_bytes()[: MAX_TEXT_BYTES + 1]
            truncated = len(raw) > MAX_TEXT_BYTES
            return {"ok": True, "path": str(target), "bytes": size,
                    "media_type": TEXTS[suffix], "truncated": truncated,
                    "text": raw[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")}

        return {"ok": False,
                "error": f"'{suffix}' is geodata, not a view — handing back its bytes "
                         "would tell you nothing you can use",
                "use_instead": ["vector_info", "raster_info", "geocache_list"]}

    return [read_artifact]
