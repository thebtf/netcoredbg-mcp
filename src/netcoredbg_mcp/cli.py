"""Command helpers for netcoredbg-mcp."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ENC_BUILD_SCRIPT_NAME = "build-netcoredbg-enc.ps1"


def run_setup_enc(script_path: str | Path | None = None) -> int:
    """Run the PowerShell build flow for an EnC-capable netcoredbg."""
    script = Path(script_path) if script_path is not None else _default_enc_build_script()
    if not script.exists():
        print(f"EnC setup script not found: {script}", file=sys.stderr)
        return 1

    powershell = _find_powershell()
    if powershell is None:
        print(
            "PowerShell not found. Install PowerShell 7 (`pwsh`) or Windows PowerShell.",
            file=sys.stderr,
        )
        return 1

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
    )
    return completed.returncode


def _find_powershell() -> str | None:
    for executable in ("pwsh", "powershell"):
        resolved = shutil.which(executable)
        if resolved:
            return resolved
    return None


def _default_enc_build_script() -> Path:
    package_script = Path(__file__).resolve().parent / "scripts" / ENC_BUILD_SCRIPT_NAME
    if package_script.exists():
        return package_script
    return Path(__file__).resolve().parents[2] / "scripts" / ENC_BUILD_SCRIPT_NAME
