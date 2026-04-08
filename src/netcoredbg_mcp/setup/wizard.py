"""Setup wizard for first-time configuration.

Orchestrates: .NET SDK check → netcoredbg download → dbgshim scan →
bridge build → config snippet output. Each step handles failure
gracefully — partial setup is better than no setup.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .home import get_home_dir

logger = logging.getLogger(__name__)


def _print(msg: str) -> None:
    """Print to stderr (stdout reserved for config output)."""
    print(msg, file=sys.stderr)


def _check_dotnet_sdk() -> bool:
    """Check if .NET SDK is available."""
    _print("\n[1/5] Checking .NET SDK...")
    try:
        result = subprocess.run(
            ["dotnet", "--version"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            _print(f"  .NET SDK {version} found")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    _print("  WARNING: .NET SDK not found — bridge build will be skipped")
    return False


def _setup_netcoredbg() -> str | None:
    """Find or download netcoredbg."""
    _print("\n[2/5] Setting up netcoredbg debugger...")

    from .netcoredbg import find_netcoredbg

    # Check if already available (env, home, PATH)
    env_path = os.environ.get("NETCOREDBG_PATH")
    if env_path and os.path.isfile(env_path):
        _print(f"  Found via NETCOREDBG_PATH: {env_path}")
        return env_path

    home_dir = get_home_dir() / "netcoredbg"
    exe_name = "netcoredbg.exe" if os.name == "nt" else "netcoredbg"

    # Check home dir
    for candidate in [home_dir / exe_name, home_dir / "netcoredbg" / exe_name]:
        if candidate.is_file():
            _print(f"  Found in home dir: {candidate}")
            return str(candidate)

    # Check PATH
    system_path = shutil.which("netcoredbg")
    if system_path:
        _print(f"  Found on PATH: {system_path}")
        return system_path

    # Download
    _print("  Downloading from Samsung GitHub...")
    from .netcoredbg import download_netcoredbg

    def progress(downloaded: int, total: int) -> None:
        if total > 0:
            pct = min(100, int(downloaded / total * 100))
            _print(f"\r  Downloading... {pct}%", )

    result = download_netcoredbg(progress_callback=progress)
    if result:
        _print(f"  Downloaded: {result}")
        return str(result)

    _print("  WARNING: Failed to download netcoredbg")
    return None


def _setup_dbgshim() -> list[str]:
    """Scan and cache dbgshim versions."""
    _print("\n[3/5] Scanning dbgshim versions...")

    from .dbgshim import extract_dbgshim_versions

    versions = extract_dbgshim_versions()
    if versions:
        _print(f"  Found {len(versions)} versions: {', '.join(sorted(versions))}")
    else:
        _print("  No .NET runtimes found — dbgshim auto-matching disabled")
    return versions


def _setup_bridge(has_dotnet: bool) -> str | None:
    """Build FlaUI bridge."""
    _print("\n[4/5] Building FlaUI bridge...")

    if os.name != "nt":
        _print("  Skipped — UI automation is Windows-only")
        return None

    if not has_dotnet:
        _print("  Skipped — requires .NET SDK")
        return None

    from .bridge import find_or_build_bridge

    result = find_or_build_bridge()
    if result:
        _print(f"  Built: {result}")
    else:
        _print("  WARNING: Bridge build failed — UI tools will be unavailable")
    return result


def _generate_config(netcoredbg_path: str | None) -> str:
    """Generate MCP configuration snippet."""
    config: dict = {
        "mcpServers": {
            "netcoredbg": {
                "command": "netcoredbg-mcp",
                "args": ["--project-from-cwd"],
            }
        }
    }
    # Only include NETCOREDBG_PATH if not in managed location
    if netcoredbg_path:
        home_dir = str(get_home_dir())
        if not netcoredbg_path.startswith(home_dir):
            config["mcpServers"]["netcoredbg"]["env"] = {
                "NETCOREDBG_PATH": netcoredbg_path,
            }

    return json.dumps(config, indent=2)


def run_setup() -> int:
    """Run the interactive setup wizard.

    Prints progress to stderr, config snippet to stdout.

    Returns:
        0 on success, 1 on critical failure.
    """
    _print("=" * 60)
    _print("  netcoredbg-mcp Setup")
    _print("=" * 60)
    _print(f"\nHome directory: {get_home_dir()}")

    # Step 1: .NET SDK
    has_dotnet = _check_dotnet_sdk()

    # Step 2: netcoredbg
    netcoredbg_path = _setup_netcoredbg()

    # Step 3: dbgshim
    dbgshim_versions = _setup_dbgshim()

    # Step 4: bridge
    bridge_path = _setup_bridge(has_dotnet)

    # Step 5: config
    _print("\n[5/5] Generating configuration...")
    config_snippet = _generate_config(netcoredbg_path)

    _print("\n" + "=" * 60)
    _print("  Setup Complete!")
    _print("=" * 60)

    summary = []
    if netcoredbg_path:
        summary.append(f"  netcoredbg: {netcoredbg_path}")
    else:
        summary.append("  netcoredbg: NOT FOUND (set NETCOREDBG_PATH manually)")
    summary.append(f"  dbgshim versions: {len(dbgshim_versions)}")
    if bridge_path:
        summary.append(f"  FlaUI bridge: {bridge_path}")
    elif os.name == "nt":
        summary.append("  FlaUI bridge: NOT BUILT")

    for line in summary:
        _print(line)

    _print("\nAdd this to your .mcp.json:")
    _print("-" * 40)
    # Output config to stdout (for piping)
    print(config_snippet)
    _print("-" * 40)

    _print("\nFor Claude Code:")
    _print("  claude mcp add --scope user netcoredbg -- netcoredbg-mcp --project-from-cwd")

    return 0 if netcoredbg_path else 1
