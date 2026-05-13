"""Detect whether a netcoredbg build supports Edit-and-Continue hooks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TypedDict


class EncSupport(TypedDict):
    supported: bool
    ncdbhook_path: str | None
    error: str | None


def detect_enc_support(netcoredbg_path: str | os.PathLike[str]) -> EncSupport:
    """Check whether ncdbhook.dll is adjacent to a netcoredbg executable."""
    path = Path(netcoredbg_path).expanduser().resolve(strict=False)
    if not path.exists():
        return {
            "supported": False,
            "ncdbhook_path": None,
            "error": f"netcoredbg not found: {path}",
        }
    if not path.is_file():
        return {
            "supported": False,
            "ncdbhook_path": None,
            "error": f"netcoredbg path is not a file: {path}",
        }

    ncdbhook_path = path.parent / "ncdbhook.dll"
    if ncdbhook_path.is_file():
        return {
            "supported": True,
            "ncdbhook_path": str(ncdbhook_path),
            "error": None,
        }

    return {
        "supported": False,
        "ncdbhook_path": None,
        "error": (
            f"ncdbhook.dll not found next to netcoredbg in {path.parent}. "
            "Build an EnC-capable netcoredbg with `netcoredbg-mcp setup --enc`."
        ),
    }
