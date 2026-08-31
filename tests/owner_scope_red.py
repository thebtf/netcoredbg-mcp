"""Test-only ownership probes for the Wave 2 command-owner matrix.

These fakes model the observable boundary that production owns: a retained Job
plus direct process/thread handles. A PID, image name, path, WMI result, or
ProcessRegistry row is only an observation and never authorizes tree cleanup.
"""

from __future__ import annotations

import asyncio
from typing import Any

from netcoredbg_mcp.windows_process_owner import DrainStatus, OwnedProcessRef, OwnerDrainReceipt


class ImmediateEofStream:
    """A stream that lets the legacy subprocess path complete deterministically."""

    async def readline(self) -> bytes:
        return b""

    async def read(self, _size: int = -1) -> bytes:
        return b""


class BlockingStream:
    """A stream held open until production cleanup cancels its observer."""

    def __init__(self) -> None:
        self.read_started = asyncio.Event()
        self.release = asyncio.Event()

    async def readline(self) -> bytes:
        self.read_started.set()
        await self.release.wait()
        return b""

    async def read(self, _size: int = -1) -> bytes:
        self.read_started.set()
        await self.release.wait()
        return b""


class PollingTimeoutStream:
    """A bounded blocked-output stand-in for ``BuildSession.cancel`` tests."""

    def __init__(self) -> None:
        self.read_started = asyncio.Event()

    async def readline(self) -> bytes:
        self.read_started.set()
        await asyncio.sleep(0.001)
        raise asyncio.TimeoutError()


class TreeProcess:
    """A process whose direct root can exit while its descendant remains alive.

    This models the fact the owner boundary must prove with Job accounting:
    root exit and a closed Job handle do not establish tree drain.  ``kill``
    and ``terminate`` intentionally affect only the root so the current
    root-process cleanup path is observable without creating OS processes.
    """

    def __init__(
        self,
        *,
        pid: int,
        stdout: Any | None = None,
        stderr: Any | None = None,
        root_exits_on_terminate: bool = True,
        initially_exited: bool = False,
    ) -> None:
        self.pid = pid
        self.stdout = stdout if stdout is not None else ImmediateEofStream()
        self.stderr = stderr if stderr is not None else ImmediateEofStream()
        self.returncode: int | None = 0 if initially_exited else None
        self.child_alive = True
        self.root_exits_on_terminate = root_exits_on_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self._root_exit = asyncio.Event()
        if initially_exited:
            self._root_exit.set()

    @property
    def active_processes(self) -> int:
        """Test-only accounting observation: root plus any live descendant."""

        return int(self.returncode is None) + int(self.child_alive)

    async def wait(self) -> int:
        self.wait_calls += 1
        await self._root_exit.wait()
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.root_exits_on_terminate:
            self.returncode = 0
            self._root_exit.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self._root_exit.set()


class OwnedCommandProcess:
    """An accepted command owner whose callers join one recorded drain result."""

    def __init__(
        self,
        process: Any,
        generation: object,
        *,
        normal_status: DrainStatus = DrainStatus.DRAINED,
    ) -> None:
        self._process = process
        self.owner = OwnedProcessRef(f"command-owner-{generation}", generation, process.pid)
        self._normal_status = normal_status
        self.receipt: OwnerDrainReceipt | None = None
        self.drain_calls: list[str] = []
        self.drain_operation_count = 0
        self.aclose_calls = 0

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def stdout(self) -> Any:
        return self._process.stdout

    @property
    def stderr(self) -> Any:
        return self._process.stderr

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait(self) -> int:
        result = await self._process.wait()
        return 0 if result is None else int(result)

    async def drain_after_grace(
        self,
        *,
        grace_timeout: float,
        force_timeout: float,
    ) -> OwnerDrainReceipt:
        del grace_timeout, force_timeout
        self.drain_calls.append("grace")
        return self._record_drain(forced=False)

    async def force_and_drain(self, *, timeout: float) -> OwnerDrainReceipt:
        del timeout
        self.drain_calls.append("force")
        return self._record_drain(forced=True)

    def _record_drain(self, *, forced: bool) -> OwnerDrainReceipt:
        if self.receipt is not None:
            return self.receipt
        self.drain_operation_count += 1
        if not forced and self._normal_status is not DrainStatus.DRAINED:
            active_processes = (
                self._process.active_processes if isinstance(self._process, TreeProcess) else None
            )
            self.receipt = OwnerDrainReceipt(
                owner=self.owner,
                status=self._normal_status,
                forced=False,
                root_returncode=self.returncode,
                active_processes=active_processes,
            )
            return self.receipt

        if isinstance(self._process, TreeProcess):
            if self._process.returncode is None:
                self._process.kill()
            self._process.child_alive = False
        self.receipt = OwnerDrainReceipt(
            owner=self.owner,
            status=DrainStatus.DRAINED,
            forced=forced,
            root_returncode=self.returncode,
            active_processes=0,
        )
        return self.receipt

    async def aclose(self) -> None:
        self.aclose_calls += 1
