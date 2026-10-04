"""`geo_python_run` starts its child without fork — the timeout must cover the whole call.

Measured 2026-10-04 (F+ Opus, `city-area-dissolve`): a snippet hung for 2 h 23 min. The
child still carried the parent's command line and had used no CPU — it never reached
exec. `subprocess.run(timeout=…)` cannot see that: the parent blocks on the fork's
error pipe before the clock it guards ever starts. Forking a process with threads
(asyncio, HTTP clients) is the known way to get there on macOS.

CPython uses posix_spawn when neither `cwd=` nor `close_fds=True` is passed, so the
runner passes the working directory to the harness instead. The first test makes the
fork path unusable: if the snippet still runs, it did not fork.
"""

from __future__ import annotations

import subprocess

import pytest

from chester import geo_python


@pytest.mark.skipif(not subprocess._USE_POSIX_SPAWN, reason="no posix_spawn here")
def test_the_snippet_runs_without_the_fork_path(tmp_path, monkeypatch):
    def no_fork(*args, **kwargs):
        raise AssertionError("geo_python forked — the hang of 2026-10-04 is possible again")

    monkeypatch.setattr(subprocess, "_fork_exec", no_fork)
    verdict = geo_python.run_geo_python("result = 6 * 7", cwd=str(tmp_path), timeout=60)
    assert verdict["ok"] is True and verdict["result"] == 42


def test_the_working_directory_still_applies(tmp_path):
    verdict = geo_python.run_geo_python(
        "import os\nresult = os.getcwd()\nopen('marker.txt', 'w').write('x')",
        cwd=str(tmp_path), timeout=60)
    assert verdict["ok"] is True
    assert (tmp_path / "marker.txt").is_file(), "a bare filename must land in the cwd"
