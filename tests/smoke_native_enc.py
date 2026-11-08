"""Opt-in real-debugger regression for sequential native EnC deltas."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, types
from mcp.shared.message import SessionMessage

from netcoredbg_mcp import windows_process_owner as native

_AUDITED_FIXTURE_SHA256 = {
    "Program.cs": "fca9f3ec57a01bad6eb437b9f99761ecb5de90a6333890dd81a5f9fc09a20fe3",
    "NativePauseApp.csproj": "0aed8be5160e596c483ee53f51d9a82d2944ff5782b0668f47b34c18708a133d",
}
_UNCONFIRMED_OWNERS: list[Any] = []


def _create_hidden_host(*, argv: Any, cwd: Any, env: Any, pipe_ends: Any) -> tuple[int, int, int]:
    import _winapi

    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESTDHANDLES | subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    startup.hStdInput, startup.hStdOutput, startup.hStdError = (
        pipe_ends.stdin_child,
        pipe_ends.stdout_child,
        pipe_ends.stderr_child,
    )
    startup.lpAttributeList = {"handle_list": pipe_ends.handle_list()}
    try:
        process, thread, pid, _ = _winapi.CreateProcess(
            native._resolve_application_name(argv[0], env),
            subprocess.list2cmdline(argv),
            None,
            None,
            True,
            native._CREATE_SUSPENDED
            | native._CREATE_UNICODE_ENVIRONMENT
            | subprocess.CREATE_NEW_CONSOLE,
            dict(env),
            cwd,
            startup,
        )
    except OSError as error:
        raise native._Win32CallError(
            native.AdmissionStage.CREATE_PROCESS, error.winerror
        ) from error
    return int(process), int(thread), int(pid)


class OwnedMcpTransport:
    """SDK streams backed by one suspended-before-admission Windows Job."""

    def __init__(self, argv: tuple[str, ...], *, cwd: str, env: dict[str, str]) -> None:
        self.argv, self.cwd, self.env = argv, cwd, env
        self.process: Any = None
        self.cleanup: dict[str, Any] = {}
        self.errors: list[str] = []
        self.tasks: list[asyncio.Task[Any]] = []
        self.streams: list[Any] = []
        self.closing = False

    async def __aenter__(self) -> tuple[Any, Any]:
        self.process = await native.WindowsOwnedProcess._launch_with(
            generation=object(),
            argv=self.argv,
            cwd=self.cwd,
            env=self.env,
            stdin_mode="pipe",
            stdout_limit=2 * 1024 * 1024,
            api=native._Kernel32(),
            pipe_ends=None,
            process_creator=_create_hidden_host,
        )
        try:
            incoming, read_stream = anyio.create_memory_object_stream(1)
            write_stream, outgoing = anyio.create_memory_object_stream(1)
            self.streams = [incoming, read_stream, write_stream, outgoing]

            async def receive() -> None:
                try:
                    while True:
                        line = await self.process.stdout.readline()
                        if not line:
                            if not self.closing:
                                raise RuntimeError("MCP host stdout closed")
                            break
                        if len(line) > 2 * 1024 * 1024 or not line.endswith(b"\n"):
                            raise RuntimeError("MCP message exceeds its framing bound")
                        await incoming.send(
                            SessionMessage(types.JSONRPCMessage.model_validate_json(line))
                        )
                except Exception as error:
                    if not self.closing:
                        self.errors.append(str(error))
                        await incoming.send(error)
                finally:
                    await incoming.aclose()

            async def send() -> None:
                try:
                    async for message in outgoing:
                        wire = (
                            message.message.model_dump_json(by_alias=True, exclude_none=True) + "\n"
                        ).encode("utf-8")
                        if len(wire) > 2 * 1024 * 1024:
                            raise RuntimeError("Outgoing MCP message exceeds its framing bound")
                        self.process.stdin.write(wire)
                        await self.process.stdin.drain()
                except Exception as error:
                    if not self.closing:
                        self.errors.append(str(error))
                        await incoming.send(error)

            async def stderr() -> None:
                while await self.process.stderr.read(65536):
                    pass

            self.tasks = [
                asyncio.create_task(receive()),
                asyncio.create_task(send()),
                asyncio.create_task(stderr()),
            ]
            return read_stream, write_stream
        except BaseException:
            await self._close()
            raise

    async def _close(self) -> None:
        self.closing = True
        try:
            if len(self.tasks) > 1:
                self.tasks[1].cancel()
                await asyncio.gather(self.tasks[1], return_exceptions=True)
            if self.process.stdin is not None:
                self.process.stdin.close()
            receipt = await self.process.drain_after_grace(grace_timeout=2, force_timeout=5)
            self.cleanup = {
                "status": receipt.status.value,
                "active_processes": receipt.active_processes,
                "owner_matches": receipt.owner == self.process.owner,
                "handles_released": False,
            }
            if (
                receipt.status is not native.DrainStatus.DRAINED
                or receipt.active_processes != 0
                or receipt.owner != self.process.owner
            ):
                raise RuntimeError(f"MCP Job cleanup unconfirmed: {self.cleanup}")
            closed = await self.process.aclose()
            if (
                closed.status is not native.DrainStatus.DRAINED
                or closed.active_processes != 0
                or closed.owner != self.process.owner
            ):
                raise RuntimeError("MCP Job close returned inconsistent accounting")
            self.cleanup["handles_released"] = True
        except BaseException:
            _UNCONFIRMED_OWNERS.append(self.process)
            raise
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            for stream in self.streams:
                await stream.aclose()

    async def __aexit__(self, error_type: Any, _error: Any, _traceback: Any) -> bool:
        cleanup = asyncio.create_task(self._close())
        cancelled = False
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                if cleanup.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        if error_type is None and self.errors:
            raise RuntimeError(f"MCP transport failed: {self.errors}")
        return False


def _coordination_root(candidate: Path) -> Path:
    override = os.environ.get("NETCOREDBG_MCP_TEST_SCRATCH_ROOT")
    if override:
        root = Path(override)
        if not root.is_absolute() or not root.is_dir():
            raise RuntimeError(
                "NETCOREDBG_MCP_TEST_SCRATCH_ROOT must name an existing absolute directory"
            )
        root = root.resolve()
        if (
            root == (candidate / ".agent").resolve()
            or (candidate / ".agent").resolve() in root.parents
        ):
            raise RuntimeError(
                "Scratch root must not be inside the candidate worktree's .agent directory"
            )
        return root
    marker = candidate / ".git"
    if marker.is_dir():
        common = marker.resolve()
    else:
        pointer = marker.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir: "):
            raise RuntimeError("Cannot resolve the linked worktree git directory")
        gitdir = (candidate / pointer[8:]).resolve()
        common_file = gitdir / "commondir"
        common = (gitdir / common_file.read_text(encoding="utf-8").strip()).resolve()
    if common.name.lower() != ".git" or not common.is_dir():
        raise RuntimeError(
            "Cannot identify the actual coordination root; set NETCOREDBG_MCP_TEST_SCRATCH_ROOT"
        )
    return common.parent


def _create_no_window(*, argv: Any, cwd: Any, env: Any, pipe_ends: Any) -> tuple[int, int, int]:
    import _winapi

    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESTDHANDLES
    startup.hStdInput, startup.hStdOutput, startup.hStdError = (
        pipe_ends.stdin_child,
        pipe_ends.stdout_child,
        pipe_ends.stderr_child,
    )
    startup.lpAttributeList = {"handle_list": pipe_ends.handle_list()}
    try:
        process, thread, pid, _ = _winapi.CreateProcess(
            native._resolve_application_name(argv[0], env),
            subprocess.list2cmdline(argv),
            None,
            None,
            True,
            native._CREATE_SUSPENDED
            | native._CREATE_UNICODE_ENVIRONMENT
            | subprocess.CREATE_NO_WINDOW,
            dict(env),
            cwd,
            startup,
        )
    except OSError as error:
        raise native._Win32CallError(
            native.AdmissionStage.CREATE_PROCESS, error.winerror
        ) from error
    return int(process), int(thread), int(pid)


async def _build(argv: tuple[str, ...], cwd: Path, env: dict[str, str]) -> None:
    owner = await native.WindowsOwnedProcess._launch_with(
        generation=object(),
        argv=argv,
        cwd=str(cwd),
        env=env,
        stdin_mode="devnull",
        api=native._Kernel32(),
        pipe_ends=None,
        process_creator=_create_no_window,
    )

    async def drain(stream: asyncio.StreamReader) -> bytes:
        captured = bytearray()
        while chunk := await stream.read(65536):
            if len(captured) < 65536:
                captured.extend(chunk[: 65536 - len(captured)])
        return bytes(captured)

    readers = [asyncio.create_task(drain(owner.stdout)), asyncio.create_task(drain(owner.stderr))]
    try:
        code = await asyncio.wait_for(owner.wait_root(), 120)
        receipt = await owner.drain_after_grace(grace_timeout=5, force_timeout=5)
        if receipt.status is not native.DrainStatus.DRAINED or receipt.active_processes != 0:
            raise RuntimeError(f"Build owner cleanup unconfirmed: {receipt}")
        output = await asyncio.wait_for(asyncio.gather(*readers), 5)
        if code:
            raise RuntimeError(
                f"Fixture build failed ({code}): {(output[0] + output[1]).decode(errors='replace')}"
            )
    finally:

        async def cleanup() -> None:
            receipt = await owner.force_and_drain(timeout=5)
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if receipt.status is not native.DrainStatus.DRAINED or receipt.active_processes != 0:
                _UNCONFIRMED_OWNERS.append(owner)
                raise RuntimeError(f"Build owner cleanup unconfirmed; scratch retained: {receipt}")
            closed = await owner.aclose()
            if closed.status is not native.DrainStatus.DRAINED or closed.active_processes != 0:
                _UNCONFIRMED_OWNERS.append(owner)
                raise RuntimeError(f"Build owner close unconfirmed; scratch retained: {closed}")

        cleanup_task = asyncio.create_task(cleanup())
        cancelled = False
        while True:
            try:
                await asyncio.shield(cleanup_task)
                break
            except asyncio.CancelledError:
                if cleanup_task.cancelled():
                    _UNCONFIRMED_OWNERS.append(owner)
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError


def _assert_gui_apphost(path: Path) -> None:
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise RuntimeError("Fixture apphost is not a PE executable")
        stream.seek(0x3C)
        pe_offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(pe_offset)
        if stream.read(4) != b"PE\0\0":
            raise RuntimeError("Fixture apphost has no PE signature")
        stream.seek(pe_offset + 24)
        magic = struct.unpack("<H", stream.read(2))[0]
        if magic not in (0x10B, 0x20B):
            raise RuntimeError("Fixture apphost has an unknown PE optional header")
        stream.seek(pe_offset + 24 + 68)
        if struct.unpack("<H", stream.read(2))[0] != 2:
            raise RuntimeError("Fixture apphost is not GUI subsystem 2")


async def run(candidate: Path, check: Callable[..., None]) -> None:
    print("\nSOURCE-NATIVE SEQUENTIAL EDIT-AND-CONTINUE")
    if os.name != "nt":
        raise RuntimeError("Native EnC smoke requires Windows")
    host = candidate / "host/NetCoreDbg.Mcp.Stateless/bin/Debug/net8.0/NetCoreDbg.Mcp.Stateless.dll"
    dotnet = os.environ.get("DOTNET_HOST_PATH") or shutil.which("dotnet")
    configured = os.environ.get("NETCOREDBG_PATH")
    debugger = str((candidate / configured).resolve()) if configured else shutil.which("netcoredbg")
    fixture = candidate / "tests/fixtures/NativePauseApp"
    for path in (
        host,
        host.with_suffix(".runtimeconfig.json"),
        fixture / "Program.cs",
        fixture / "NativePauseApp.csproj",
    ):
        if not path.is_file():
            raise RuntimeError(f"Required native EnC smoke prerequisite is absent: {path}")
    if not dotnet or not Path(dotnet).is_file():
        raise RuntimeError("dotnet is required through DOTNET_HOST_PATH or PATH")
    if (
        not debugger
        or not Path(debugger).is_file()
        or not (Path(debugger).parent / "ncdbhook.dll").is_file()
    ):
        raise RuntimeError("EnC-enabled netcoredbg and adjacent ncdbhook.dll are required")
    snapshots = {
        name: (fixture / name).read_text(encoding="utf-8-sig") for name in _AUDITED_FIXTURE_SHA256
    }
    for name, content in snapshots.items():
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != _AUDITED_FIXTURE_SHA256[name]:
            raise RuntimeError(
                f"Fixture changed; re-audit it for UI and input effects before running: {name}"
            )
    scratch = Path(tempfile.mkdtemp(prefix="native-enc-smoke-", dir=_coordination_root(candidate)))
    safe_to_remove = False
    transport: OwnedMcpTransport | None = None
    try:
        project = scratch / "fixture"
        project.mkdir()
        for name, content in snapshots.items():
            (project / name).write_text(content, encoding="utf-8", newline="")
        source = project / "Program.cs"
        lines = source.read_text(encoding="utf-8-sig").splitlines()

        def unique_line(text: str) -> int:
            found = [i for i, line in enumerate(lines, 1) if text in line]
            if len(found) != 1:
                raise RuntimeError(f"Expected exactly one fixture source marker: {text}")
            return found[0]

        main_line = unique_line("NATIVE_PAUSE_BREAKPOINT")
        edit_line = unique_line("iteration = unchecked(iteration + 1);")
        break_line = unique_line("if (iteration % 20 == 0)")
        env = os.environ.copy()
        for key in ("NETCOREDBG_PROJECT_ROOT", "MCP_PROJECT_ROOT"):
            env.pop(key, None)
        env.update(
            NETCOREDBG_PATH=str(Path(debugger).resolve()),
            TEMP=str(scratch),
            TMP=str(scratch),
            DOTNET_CLI_TELEMETRY_OPTOUT="1",
            DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1",
            DOTNET_ADD_GLOBAL_TOOLS_TO_PATH="false",
            DOTNET_CLI_HOME=str(scratch),
        )
        await _build(
            (
                str(dotnet),
                "build",
                str(project / "NativePauseApp.csproj"),
                "-c",
                "Debug",
                "--disable-build-servers",
                "-p:OutputType=WinExe",
                "-p:UseAppHost=true",
            ),
            project,
            env,
        )
        output = project / "bin/Debug/net8.0"
        apphost = output / "NativePauseApp.exe"
        _assert_gui_apphost(apphost)
        alias = output / "OwnedFixture.exe"
        if alias.with_suffix(".dll").exists():
            raise RuntimeError("Unexpected matching DLL for fixture alias")
        shutil.copy2(apphost, alias)
        _assert_gui_apphost(alias)
        transport = OwnedMcpTransport(
            (str(dotnet), str(host), "--legacy-compat", "--project", str(project)),
            cwd=str(project),
            env=env,
        )
        async with transport as (read_stream, write_stream):
            async with ClientSession(
                read_stream, write_stream, read_timeout_seconds=timedelta(seconds=90)
            ) as client:
                await client.initialize()

                async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
                    result = await client.call_tool(name, args)
                    data = result.structuredContent
                    if result.isError or not isinstance(data, dict) or data.get("error"):
                        raise RuntimeError(f"{name} failed: {data}")
                    return data

                stop_confirmed = False
                launch_attempted = False
                try:
                    for line in (main_line, break_line):
                        added = await call("add_breakpoint", {"file": str(source), "line": line})
                        if not isinstance(added.get("data"), dict):
                            raise RuntimeError(f"Could not register breakpoint: {added}")
                    launch_attempted = True
                    started = await call("start_debug", {"program": str(alias), "pre_build": False})
                    if started.get("data", {}).get("success") is not True:
                        raise RuntimeError(f"Debug launch failed: {started}")

                    async def wait_stopped() -> None:
                        async def observe() -> None:
                            while True:
                                state = await call("get_debug_state", {})
                                if state.get("state") == "stopped":
                                    return
                                if state.get("state") in ("idle", "terminated"):
                                    raise RuntimeError(f"Debuggee stopped unexpectedly: {state}")
                                await asyncio.sleep(0.1)

                        await asyncio.wait_for(observe(), 30)

                    await wait_stopped()
                    stack = await call("get_call_stack", {})
                    frames = stack.get("data", {}).get("frames", [])
                    if not frames or frames[0].get("line") != main_line:
                        raise RuntimeError(f"Expected the Main breakpoint before RunLoop: {stack}")
                    for value in (100, 200):
                        edit = lines[edit_line - 1].replace("+ 1)", f"+ {value})")
                        applied = await call(
                            "apply_code_change",
                            {
                                "file": str(source),
                                "edits": [
                                    {
                                        "start_line": edit_line,
                                        "end_line": edit_line,
                                        "new_text": edit,
                                    }
                                ],
                            },
                        )
                        if (
                            applied.get("state") != "stopped"
                            or applied.get("data", {}).get("success") is not True
                        ):
                            raise RuntimeError(f"EnC delta was not applied: {applied}")
                    continued = await call("continue_execution", {})
                    if (
                        continued.get("state") != "stopped"
                        or continued.get("reason") != "breakpoint"
                        or continued.get("timed_out")
                    ):
                        raise RuntimeError(f"Expected the loop breakpoint: {continued}")
                    stack = await call("get_call_stack", {})
                    frames = stack.get("data", {}).get("frames", [])
                    if (
                        not frames
                        or frames[0].get("line") != break_line
                        or "RunLoop" not in frames[0].get("name", "")
                    ):
                        raise RuntimeError(f"Expected the RunLoop breakpoint: {stack}")
                    evaluated = await call(
                        "evaluate_expression",
                        {"expression": "iteration", "frame_id": frames[0]["id"]},
                    )
                    if evaluated.get("data", {}).get("result") != "200":
                        raise RuntimeError(f"Expected iteration=200: {evaluated}")
                    check("Native sequential EnC executes iteration=200", True)
                finally:
                    if launch_attempted:
                        stopped = await call("stop_debug", {})
                        if (
                            stopped.get("state") != "idle"
                            or stopped.get("data", {}).get("success") is not True
                        ):
                            raise RuntimeError(f"Public stop did not confirm idle: {stopped}")
                        stop_confirmed = True
                if not stop_confirmed:
                    raise RuntimeError("Public stop was not confirmed")
        cleanup = transport.cleanup
        if (
            cleanup.get("status") != "drained"
            or cleanup.get("active_processes") != 0
            or not cleanup.get("handles_released")
            or not cleanup.get("owner_matches")
        ):
            raise RuntimeError(f"Owned MCP cleanup unconfirmed: {cleanup}")
        check("Native EnC public stop and owned Job drained", True)
        safe_to_remove = True
    finally:
        if safe_to_remove:
            shutil.rmtree(scratch)
        else:
            print(f"Native EnC scratch retained (cleanup or scenario unconfirmed): {scratch}")
