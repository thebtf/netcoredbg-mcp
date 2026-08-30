"""Controlled root/descendant fixture for owner-scope RED tests.

The fixture writes exact freshly-created process IDs only so a test can clean
up after an intentionally failing assertion.  Those IDs are never ownership
proof: production authority must come from retained Job/process handles, not a
PID observed by this test harness.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def _wait_for_release(path: Path) -> None:
    while not path.exists():
        time.sleep(0.01)


def child(marker: Path, release: Path) -> int:
    marker.write_text(json.dumps({"pid": __import__("os").getpid()}), encoding="utf-8")
    _wait_for_release(release)
    return 0


def root(marker: Path, child_marker: Path, release: Path) -> int:
    spawned = subprocess.Popen([sys.executable, __file__, "child", str(child_marker), str(release)])
    marker.write_text(
        json.dumps({"pid": __import__("os").getpid(), "child_pid": spawned.pid}),
        encoding="utf-8",
    )
    _wait_for_release(release)
    spawned.wait(timeout=5)
    return 0


def main(argv: list[str]) -> int:
    mode, *paths = argv
    if mode == "child" and len(paths) == 2:
        return child(Path(paths[0]), Path(paths[1]))
    if mode == "root" and len(paths) == 3:
        return root(Path(paths[0]), Path(paths[1]), Path(paths[2]))
    raise SystemExit("expected child <marker> <release> or root <marker> <child-marker> <release>")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
