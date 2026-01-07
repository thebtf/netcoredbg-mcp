"""Process cleanup for build operations.

Kills processes that might be holding locks on build output files.
This is necessary before rebuild when debugging sessions may be active.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Known process names that might hold locks on .NET build outputs
KNOWN_DEBUGGER_PROCESSES = frozenset({
    "netcoredbg",
    "netcoredbg.exe",
    "dotnet",
    "dotnet.exe",
})


async def kill_processes_in_directory(
    directory: str,
    include_children: bool = True,
    timeout: float = 5.0,
) -> int:
    """Kill processes whose executable is inside the given directory.

    This finds any process running from bin/Debug or bin/Release and terminates it.

    Args:
        directory: Directory path (e.g., project bin/Debug folder)
        include_children: Also kill child processes
        timeout: Timeout for kill operations

    Returns:
        Number of processes killed
    """
    if os.name != "nt":
        return await _kill_processes_unix(directory, timeout)
    return await _kill_processes_windows(directory, timeout)


async def _kill_processes_windows(directory: str, timeout: float) -> int:
    """Kill processes on Windows using taskkill and WMIC."""
    killed = 0
    dir_path = Path(directory).resolve()

    try:
        # Use WMIC to find processes with executable paths in the directory
        # WMIC is deprecated but works reliably on all Windows versions
        result = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "wmic",
                "process",
                "get",
                "ProcessId,ExecutablePath",
                "/format:csv",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout,
        )
        stdout, _ = await result.communicate()

        pids_to_kill = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.strip().split(",")
            if len(parts) >= 3:
                # CSV format: Node,ExecutablePath,ProcessId
                exe_path = parts[1] if len(parts) > 1 else ""
                pid_str = parts[-1]

                if exe_path and pid_str.isdigit():
                    try:
                        exe_resolved = Path(exe_path).resolve()
                        if str(exe_resolved).lower().startswith(str(dir_path).lower()):
                            pids_to_kill.append(int(pid_str))
                    except (OSError, ValueError):
                        continue

        # Kill found processes
        for pid in pids_to_kill:
            try:
                await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        "taskkill",
                        "/F",
                        "/PID",
                        str(pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    ),
                    timeout=2.0,
                )
                logger.info(f"Killed process PID {pid}")
                killed += 1
            except Exception as e:
                logger.warning(f"Failed to kill PID {pid}: {e}")

    except asyncio.TimeoutError:
        logger.warning("Process enumeration timed out")
    except Exception as e:
        logger.warning(f"Process cleanup failed: {e}")

    return killed


async def _kill_processes_unix(directory: str, timeout: float) -> int:
    """Kill processes on Unix using lsof or /proc."""
    killed = 0
    dir_path = Path(directory).resolve()

    try:
        # Use lsof to find processes with files open in directory
        result = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "lsof",
                "+D",
                str(dir_path),
                "-t",  # Output only PIDs
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            ),
            timeout=timeout,
        )
        stdout, _ = await result.communicate()

        pids = set()
        for line in stdout.decode().splitlines():
            if line.strip().isdigit():
                pids.add(int(line.strip()))

        # Kill found processes
        for pid in pids:
            try:
                os.kill(pid, 9)  # SIGKILL
                logger.info(f"Killed process PID {pid}")
                killed += 1
            except (ProcessLookupError, PermissionError) as e:
                logger.warning(f"Failed to kill PID {pid}: {e}")

    except FileNotFoundError:
        logger.debug("lsof not available, skipping Unix process cleanup")
    except asyncio.TimeoutError:
        logger.warning("Process enumeration timed out")
    except Exception as e:
        logger.warning(f"Process cleanup failed: {e}")

    return killed


async def kill_debugger_processes(
    program_path: str | None = None,
    netcoredbg_pid: int | None = None,
    kill_all_netcoredbg: bool = False,
    timeout: float = 5.0,
) -> int:
    """Kill debugger-related processes.

    Args:
        program_path: Path to the debugged program (kills that specific process)
        netcoredbg_pid: PID of netcoredbg to kill
        kill_all_netcoredbg: Kill ALL netcoredbg.exe processes system-wide
        timeout: Timeout for operations

    Returns:
        Number of processes killed
    """
    killed = 0

    # Kill ALL netcoredbg processes (aggressive cleanup for rebuild)
    if kill_all_netcoredbg:
        try:
            if os.name == "nt":
                proc = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/IM", "netcoredbg.exe",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=2.0)
                logger.info("Killed all netcoredbg.exe processes")
                killed += 1
            else:
                proc = await asyncio.create_subprocess_exec(
                    "pkill", "-9", "netcoredbg",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=2.0)
                killed += 1
        except Exception as e:
            logger.debug(f"Failed to kill netcoredbg processes: {e}")

    # Kill specific netcoredbg PID if provided
    if netcoredbg_pid:
        try:
            if os.name == "nt":
                proc = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/PID", str(netcoredbg_pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            else:
                os.kill(netcoredbg_pid, 9)
            logger.info(f"Killed netcoredbg PID {netcoredbg_pid}")
            killed += 1
        except Exception as e:
            logger.debug(f"Failed to kill netcoredbg PID {netcoredbg_pid}: {e}")

    # Kill process by program path
    if program_path:
        program_name = Path(program_path).name
        try:
            if os.name == "nt":
                proc = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/IM", program_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=2.0)
                killed += 1
                logger.info(f"Killed processes: {program_name}")
            else:
                proc = await asyncio.create_subprocess_exec(
                    "pkill", "-9", "-f", program_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=2.0)
                killed += 1
        except Exception as e:
            logger.debug(f"Failed to kill {program_name}: {e}")

    return killed


def _find_project_directories(root_path: Path) -> list[Path]:
    """Find all directories containing .csproj or .fsproj files.

    For solutions, we need to scan all project subdirectories to find bin/obj folders.

    Args:
        root_path: Root directory to search from

    Returns:
        List of directories containing project files
    """
    project_dirs = set()

    # Check if root is a solution or project file
    if root_path.is_file():
        if root_path.suffix.lower() == ".sln":
            # Solution file - search for projects in all subdirectories
            root_path = root_path.parent
        else:
            # Single project file
            return [root_path.parent]

    # Find all project files recursively
    for pattern in ["**/*.csproj", "**/*.fsproj", "**/*.vbproj"]:
        for proj_file in root_path.glob(pattern):
            project_dirs.add(proj_file.parent)

    # If no projects found, assume root is project directory
    if not project_dirs:
        project_dirs.add(root_path)

    return list(project_dirs)


async def cleanup_for_build(
    project_path: str,
    program_path: str | None = None,
    netcoredbg_pid: int | None = None,
    configurations: list[str] | None = None,
    kill_all_netcoredbg: bool = True,
) -> int:
    """Full cleanup before build operation.

    Kills:
    1. ALL netcoredbg processes system-wide (unless disabled)
    2. Specific netcoredbg PID if provided
    3. The debugged program by name if provided
    4. Any process running from bin/Debug or bin/Release directories

    For solution files, scans ALL project subdirectories.

    Args:
        project_path: Path to project/solution file or directory
        program_path: Path to debugged program (optional)
        netcoredbg_pid: PID of netcoredbg (optional)
        configurations: Build configurations to clean (default: Debug, Release)
        kill_all_netcoredbg: Kill all netcoredbg processes (default True)

    Returns:
        Total processes killed
    """
    total_killed = 0

    # Kill debugger processes (including all netcoredbg by default)
    total_killed += await kill_debugger_processes(
        program_path=program_path,
        netcoredbg_pid=netcoredbg_pid,
        kill_all_netcoredbg=kill_all_netcoredbg,
    )

    # Find all project directories (important for solutions with multiple projects)
    project_dirs = _find_project_directories(Path(project_path))
    logger.debug(f"Found {len(project_dirs)} project directories to clean")

    # Kill processes in output directories of all projects
    configs = configurations or ["Debug", "Release"]
    for project_dir in project_dirs:
        for config in configs:
            # Check bin directory and all target framework subdirs
            bin_dir = project_dir / "bin" / config
            if bin_dir.exists():
                total_killed += await kill_processes_in_directory(str(bin_dir))
                # Also check framework-specific subdirectories (net6.0, net8.0, etc.)
                for subdir in bin_dir.iterdir():
                    if subdir.is_dir():
                        total_killed += await kill_processes_in_directory(str(subdir))

            # Also check obj directory
            obj_dir = project_dir / "obj" / config
            if obj_dir.exists():
                total_killed += await kill_processes_in_directory(str(obj_dir))

    if total_killed > 0:
        # Give OS time to release handles
        await asyncio.sleep(0.5)
        logger.info(f"Cleanup complete: {total_killed} processes terminated")

    return total_killed
