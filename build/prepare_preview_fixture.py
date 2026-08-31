#!/usr/bin/env python3
"""Create the local preview artifact consumed by the fixed coverage test project."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from collections.abc import Sequence
from pathlib import Path

_EXECUTABLE_NAME = "netcoredbg-mcp-stateless-preview.exe"
_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)-preview\.(?:[1-9][0-9]*)$"
)
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_fixture(executable: Path, output: Path, commit: str, version: str) -> None:
    if not _SHA_PATTERN.fullmatch(commit):
        raise ValueError("commit must be a lowercase 40-character Git SHA")
    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("version must use x.y.z-preview.n form")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"fixture output already exists: {output}")
    executable_bytes = executable.read_bytes()
    if not executable_bytes:
        raise ValueError("preview executable is empty")

    output.mkdir(parents=True)
    archive_name = f"netcoredbg-mcp-stateless-preview-win-x64-{version}.zip"
    manifest_name = f"netcoredbg-mcp-stateless-preview-win-x64-{version}.manifest.json"
    archive_path = output / archive_name
    entry = zipfile.ZipInfo(_EXECUTABLE_NAME, date_time=(1980, 1, 1, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_STORED
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(entry, executable_bytes)
    archive_bytes = archive_path.read_bytes()

    manifest = {
        "schema_version": "1.0",
        "version": version,
        "tag": f"stateless-preview-v{version}",
        "commit": commit,
        "rid": "win-x64",
        "archive": {
            "name": archive_name,
            "size_bytes": len(archive_bytes),
            "sha256": _sha256(archive_bytes),
        },
        "executable": {
            "name": _EXECUTABLE_NAME,
            "size_bytes": len(executable_bytes),
            "sha256": _sha256(executable_bytes),
        },
    }
    (output / manifest_name).write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version", default="0.0.0-preview.1")
    arguments = parser.parse_args(argv)
    build_fixture(arguments.executable, arguments.output, arguments.commit, arguments.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
