"""Launch custody for an ordinary POSIX adapter process group.

The private, single-threaded guardian is the group's live session leader. Only it
signals that group; the server never turns an observed PID into signal authority.
The adapter inherits direct stdio, but neither end of the private control socket.
Group-escaped/daemonized descendants and unexpected guardian loss are not covered.
"""

from __future__ import annotations

import asyncio
import errno
import json
import math
import os
import select
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_DEFAULT_FORCE_TIMEOUT = 2.0
_launch_reclaimers: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class PosixCleanupResult:
    """Protocol/root completion, not a census of all processes in the group.

    complete requires a reported adapter exit, successful group SIGTERM, and the
    guardian's expected final self-group SIGKILL. It does not claim Job-style
    accounting or that an escaped descendant was terminated.
    """

    root_returncode: int | None
    guardian_returncode: int | None
    group_signal_sent: bool
    complete: bool


class PosixOwnedProcess:
    """One launch-created owner; its PID and exit status belong to the adapter."""

    def __init__(
        self,
        generation: object,
        guardian: asyncio.subprocess.Process,
        control_reader: asyncio.StreamReader,
        control_writer: asyncio.StreamWriter,
    ) -> None:
        assert guardian.stdout is not None and guardian.stderr is not None
        self.generation = generation
        self.stdin = guardian.stdin
        self.stdout = guardian.stdout
        self.stderr = guardian.stderr
        self._guardian = guardian
        # asyncio exposes pipe transports through the retained launch transport.
        transport = getattr(guardian, "_transport")
        self._pipe_transports = tuple(transport.get_pipe_transport(fd) for fd in (1, 2))
        self._control_reader = control_reader
        self._control_writer = control_writer
        self._pid = 0
        self._returncode: int | None = None
        self._root_exit = asyncio.Event()
        self._started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._group_signal_sent = False
        self._force_armed = False
        self._control_failed = False
        self._control_eof = False
        self._cleanup_task: asyncio.Task[PosixCleanupResult] | None = None
        self._result: PosixCleanupResult | None = None
        self._closed = False
        self._messages = asyncio.create_task(self._read_messages())

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def returncode(self) -> int | None:
        return self._returncode

    @classmethod
    async def launch(
        cls,
        generation: object,
        argv: Sequence[str],
        cwd: str | None,
        env: Mapping[str, str] | None,
        stdin_mode: Literal["pipe", "devnull"] = "pipe",
    ) -> PosixOwnedProcess:
        if os.name != "posix":
            raise RuntimeError("PosixOwnedProcess is available only on POSIX")
        if not argv:
            raise ValueError("argv must contain an executable")
        if stdin_mode not in ("pipe", "devnull"):
            raise ValueError("stdin_mode must be pipe or devnull")
        configuration = (
            json.dumps(
                {
                    "argv": list(argv),
                    "cwd": cwd,
                }
            ).encode()
            + b"\n"
        )
        environment = dict(env) if env is not None else None
        # Shield the entire admission: cancellation must not lose a spawned child
        # between create_subprocess_exec and receipt of the adapter handshake.
        admission = asyncio.create_task(
            cls._launch(
                generation,
                configuration,
                stdin_mode,
                environment,
            )
        )
        try:
            return await asyncio.shield(admission)
        except asyncio.CancelledError:
            reclaimer = asyncio.create_task(_reclaim_launch(admission))
            _launch_reclaimers.add(reclaimer)
            reclaimer.add_done_callback(_launch_reclaimers.discard)
            await asyncio.shield(reclaimer)
            raise

    @classmethod
    async def _launch(
        cls,
        generation: object,
        configuration: bytes,
        stdin_mode: Literal["pipe", "devnull"],
        environment: dict[str, str] | None,
    ) -> PosixOwnedProcess:
        parent, child = socket.socketpair()
        parent.setblocking(False)
        owner: PosixOwnedProcess | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.open_connection(sock=parent)
            guardian = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--guardian",
                str(child.fileno()),
                stdin=asyncio.subprocess.PIPE
                if stdin_mode == "pipe"
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(child.fileno(),),
                start_new_session=True,
                env=environment,
            )
            child.close()
            owner = cls(generation, guardian, reader, writer)
            writer.write(configuration)
            await writer.drain()
            await owner._started
            return owner
        except BaseException:
            if writer is not None:
                # EOF is itself a cleanup request, including failed admission.
                writer.close()
            else:
                parent.close()
            if owner is not None:
                await asyncio.shield(owner.aclose())
            raise
        finally:
            child.close()

    async def wait(self) -> int:
        """Wait for the real adapter, independently of its descendants' stdio."""

        await self._root_exit.wait()
        if self._returncode is None:
            raise RuntimeError("POSIX guardian lost before reporting the adapter exit")
        return self._returncode

    async def cleanup(self, grace_timeout: float, force_timeout: float) -> PosixCleanupResult:
        """Join one cleanup; a cancelled waiter does not cancel the retained owner."""

        if any(not math.isfinite(value) or value < 0 for value in (grace_timeout, force_timeout)):
            raise ValueError("cleanup timeouts must be finite and non-negative")
        if self._result is not None and self._result.complete:
            return self._result
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup(grace_timeout, force_timeout))
        return await asyncio.shield(self._cleanup_task)

    async def _cleanup(self, grace_timeout: float, force_timeout: float) -> PosixCleanupResult:
        if self._guardian.returncode is None and not self._control_eof:
            self._control_failed = False
            try:
                self._control_writer.write(
                    json.dumps(
                        {
                            "command": "cleanup",
                            "grace_timeout": grace_timeout,
                            "force_timeout": force_timeout,
                        }
                    ).encode()
                    + b"\n"
                )
                await self._control_writer.drain()
            except (OSError, RuntimeError):
                self._control_failed = True
        deadline = asyncio.get_running_loop().time() + grace_timeout + force_timeout + 1.0
        # Process.wait() can also wait for descendant-held stdout pipes. Only the
        # guardian's child-watcher status is relevant here, not those pipes' EOF.
        while self._guardian.returncode is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        if self._guardian.returncode is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._messages), 1.0)
            except asyncio.TimeoutError:
                pass
        self._result = PosixCleanupResult(
            root_returncode=self._returncode,
            guardian_returncode=self._guardian.returncode,
            group_signal_sent=self._group_signal_sent,
            complete=(
                self._returncode is not None
                and self._group_signal_sent
                and self._force_armed
                and not self._control_failed
                and self._guardian.returncode == -signal.SIGKILL
            ),
        )
        return self._result

    async def aclose(self) -> PosixCleanupResult:
        """Close local transports only after cleanup, or a confirmed guardian loss."""

        result = await self.cleanup(0.0, _DEFAULT_FORCE_TIMEOUT)
        if self._closed or (not result.complete and self._guardian.returncode is None):
            return result
        self._closed = True
        self._control_writer.close()
        if self.stdin is not None:
            self.stdin.close()
        # Close pipe transports, not Process.terminate/kill or the subprocess
        # transport's live-process close path. Escaped stdout holders are not owned.
        for transport in self._pipe_transports:
            if transport is not None:
                transport.close()
        return result

    async def _read_messages(self) -> None:
        try:
            while line := await self._control_reader.readline():
                message = json.loads(line)
                event = message["event"]
                if event == "started":
                    pid = message["pid"]
                    if type(pid) is not int or pid <= 0 or self._started.done():
                        raise ValueError("invalid adapter handshake")
                    self._pid = pid
                    self._started.set_result(None)
                elif event == "root_exit":
                    code = message["returncode"]
                    if type(code) is not int or self._returncode is not None:
                        raise ValueError("invalid adapter exit status")
                    self._returncode = code
                    self._root_exit.set()
                elif event == "group_signal_sent":
                    self._group_signal_sent = True
                    self._control_failed = False
                elif event == "force_group_armed":
                    self._force_armed = True
                elif event == "launch_error":
                    self._started.set_exception(
                        OSError(
                            message["errno"],
                            message["message"],
                            message.get("filename"),
                        )
                    )
                elif event == "cleanup_error":
                    self._control_failed = True
                else:
                    raise ValueError("unknown guardian event")
        except (OSError, ValueError, KeyError, TypeError, asyncio.IncompleteReadError):
            self._control_failed = True
        finally:
            self._control_eof = True
            self._root_exit.set()
            if not self._started.done():
                self._started.set_exception(RuntimeError("POSIX guardian admission failed"))


async def _reclaim_launch(admission: asyncio.Task[PosixOwnedProcess]) -> None:
    try:
        owner = await admission
    except (Exception, asyncio.CancelledError):
        return
    await owner.aclose()


def _notify(control: socket.socket, event: str, **fields: object) -> None:
    try:
        control.sendall(json.dumps({"event": event, **fields}).encode() + b"\n")
    except OSError:
        # A vanished server must not prevent cleanup by its surviving guardian.
        pass


def _close_guardian_stdio() -> None:
    for descriptor in (0, 1, 2):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _guardian_cleanup(
    control: socket.socket,
    root: subprocess.Popen[bytes],
    report_root: Callable[[], None],
    grace_timeout: float,
    force_timeout: float,
) -> None:
    if not _has_private_session():
        _notify(control, "cleanup_error")
        return
    try:
        # Zero means OUR current group; its leader is still this live process.
        os.killpg(0, signal.SIGTERM)
        _notify(control, "group_signal_sent")
        deadline = time.monotonic() + grace_timeout
        while time.monotonic() < deadline:
            report_root()
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        if root.poll() is None:
            # This single-threaded helper is the only reaper. A running or zombie
            # direct child still reserves its PID until OUR wait/poll reaps it.
            # No post-reap PID acquisition or signalling is permitted.
            os.kill(root.pid, signal.SIGKILL)
            try:
                root.wait(timeout=force_timeout)
            except subprocess.TimeoutExpired:
                pass
        report_root()
        _notify(control, "force_group_armed")
        os.killpg(0, signal.SIGKILL)
    except OSError:
        _notify(control, "cleanup_error")
        # Keep launch custody alive for a later cleanup retry; no numerical fallback.


def _has_private_session() -> bool:
    return os.getpid() == os.getsid(0) == os.getpgrp()


def _guardian_main(control_fd: int) -> int:
    control = socket.socket(fileno=control_fd)
    control.set_inheritable(False)
    if not _has_private_session():
        _close_guardian_stdio()
        _notify(
            control,
            "launch_error",
            errno=errno.EPERM,
            message="private POSIX guardian requires its own session",
            filename=None,
        )
        return 1
    # Caught handlers reset at adapter exec; SIG_IGN would wrongly survive there.
    signal.signal(signal.SIGTERM, lambda *_args: None)
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    pending = b""
    while b"\n" not in pending:
        chunk = control.recv(65536)
        if not chunk:
            _close_guardian_stdio()
            return 0
        pending += chunk
    configuration_line, pending = pending.split(b"\n", 1)
    try:
        configuration = json.loads(configuration_line)
        root = subprocess.Popen(
            configuration["argv"],
            cwd=configuration["cwd"],
            close_fds=True,
        )
    except (OSError, ValueError, TypeError, KeyError) as error:
        _close_guardian_stdio()
        _notify(
            control,
            "launch_error",
            errno=getattr(error, "errno", None),
            message=str(error)[:1024],
            filename=getattr(error, "filename", None),
        )
        return 1
    _close_guardian_stdio()
    _notify(control, "started", pid=root.pid)
    reported = False

    def report_root() -> None:
        nonlocal reported
        code = root.poll()
        if code is not None and not reported:
            reported = True
            _notify(control, "root_exit", returncode=code)

    while True:
        report_root()
        if b"\n" not in pending:
            readable, _, _ = select.select([control], [], [], 0.05)
            if not readable:
                continue
            try:
                chunk = control.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                _guardian_cleanup(control, root, report_root, 0.1, _DEFAULT_FORCE_TIMEOUT)
                continue
            pending += chunk
            if b"\n" not in pending:
                continue
        line, pending = pending.split(b"\n", 1)
        try:
            command = json.loads(line)
            if command["command"] != "cleanup":
                raise ValueError("unknown guardian command")
            grace = float(command["grace_timeout"])
            force = float(command["force_timeout"])
            if any(not math.isfinite(value) or value < 0 for value in (grace, force)):
                raise ValueError("invalid guardian deadlines")
        except (ValueError, TypeError, KeyError):
            _notify(control, "cleanup_error")
            continue
        _guardian_cleanup(control, root, report_root, grace, force)


if __name__ == "__main__":
    if os.name != "posix" or len(sys.argv) != 3 or sys.argv[1] != "--guardian":
        raise SystemExit("private POSIX guardian entry point")
    raise SystemExit(_guardian_main(int(sys.argv[2])))
