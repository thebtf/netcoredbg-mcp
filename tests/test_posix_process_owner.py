"""Native POSIX ownership checks; runnable with unittest without third-party packages.

From the candidate checkout: PYTHONPATH=src python3 tests/test_posix_process_owner.py -v
The controlled fixtures have finite lifetimes; test cleanup never signals a stored PID.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import netcoredbg_mcp.posix_process_owner as owner_module
from netcoredbg_mcp.posix_process_owner import PosixOwnedProcess

_CHILD = r"""
import json, os, signal, stat, sys, time
from pathlib import Path
root = Path(sys.argv[1])
mode = sys.argv[2]
def sockets():
    result = []
    for fd in range(3, 256):
        try:
            if stat.S_ISSOCK(os.fstat(fd).st_mode):
                result.append(fd)
        except OSError:
            pass
    return result
info = {
    "pid": os.getpid(), "group": os.getpgrp(), "session": os.getsid(0),
    "sockets": sockets(),
}
def stopped(*_args):
    (root / "child.term").write_text("terminated")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN if mode == "stubborn" else stopped)
signal.alarm(15)
(root / "pulse").write_text("0")
(root / "child.json").write_text(json.dumps(info))
for count in range(1, 1500):
    staging = root / "pulse.tmp"
    staging.write_text(str(count))
    staging.replace(root / "pulse")
    time.sleep(0.01)
"""

_ADAPTER = r"""
import json, os, signal, stat, subprocess, sys, time
from pathlib import Path
root = Path(os.environ["OWNER_FIXTURE_DIR"])
mode = os.environ["OWNER_FIXTURE_MODE"]
signal.alarm(15)
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
def sockets():
    result = []
    for fd in range(3, 256):
        try:
            if stat.S_ISSOCK(os.fstat(fd).st_mode):
                result.append(fd)
        except OSError:
            pass
    return result
child = None
if mode in ("tree", "exit", "stubborn"):
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", os.environ["OWNER_CHILD_SOURCE"], str(root), mode],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.monotonic() + 5
    while not (root / "child.json").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("child did not become ready")
        time.sleep(0.01)
info = {
    "pid": os.getpid(), "parent": os.getppid(), "group": os.getpgrp(),
    "session": os.getsid(0), "sockets": sockets(), "cwd": os.getcwd(),
    "value": os.environ.get("OWNER_VALUE"), "child": child.pid if child else None,
}
(root / "root.json").write_text(json.dumps(info))
print(json.dumps(info), flush=True)
print("adapter-stderr", file=sys.stderr, flush=True)
if mode == "exit":
    raise SystemExit(23)
for line in sys.stdin.buffer:
    if line == b"exit\n":
        raise SystemExit(17)
    if line == b"crash-group\n":
        os.killpg(0, signal.SIGKILL)
    sys.stdout.buffer.write(b"echo:" + line)
    sys.stdout.buffer.flush()
"""

_CONTROLLER = r"""
import asyncio, json, os, sys
from netcoredbg_mcp.posix_process_owner import PosixOwnedProcess
async def main():
    owner = await PosixOwnedProcess.launch(
        generation="controller", argv=[sys.executable, "-u", "-c", os.environ["OWNER_ADAPTER"]],
        cwd=os.environ["OWNER_FIXTURE_DIR"], env=os.environ,
    )
    print((await owner.stdout.readline()).decode().strip(), flush=True)
    if os.environ["OWNER_FIXTURE_MODE"] == "exit":
        await owner.wait()
    os._exit(0)
asyncio.run(main())
"""


@unittest.skipUnless(os.name == "posix", "native POSIX process/session proof")
class PosixProcessOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="posix-owner-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.owners: list[PosixOwnedProcess] = []
        self.addAsyncCleanup(self._close_owners)

    async def _close_owners(self) -> None:
        for owner in self.owners:
            await owner.aclose()

    def _environment(self, mode: str, directory: Path | None = None) -> dict[str, str]:
        result = dict(os.environ)
        result.update(
            OWNER_FIXTURE_DIR=str(directory or self.root),
            OWNER_FIXTURE_MODE=mode,
            OWNER_CHILD_SOURCE=_CHILD,
            OWNER_VALUE="private-environment",
        )
        return result

    async def _launch(self, mode: str = "echo", directory: Path | None = None):
        owner = await PosixOwnedProcess.launch(
            generation=object(),
            argv=[sys.executable, "-u", "-c", _ADAPTER],
            cwd=str(directory or self.root),
            env=self._environment(mode, directory),
        )
        self.owners.append(owner)
        info = json.loads(await asyncio.wait_for(owner.stdout.readline(), 5))
        return owner, info

    async def _pulse_stops(self, directory: Path | None = None) -> None:
        pulse = (directory or self.root) / "pulse"
        for _ in range(50):
            try:
                before = pulse.read_text()
            except FileNotFoundError:
                await asyncio.sleep(0.02)
                continue
            await asyncio.sleep(0.12)
            if pulse.read_text() == before:
                return
        self.fail("ordinary-group child kept executing after cleanup")

    async def test_real_adapter_identity_direct_streams_and_environment(self) -> None:
        owner, info = await self._launch()
        self.assertEqual(owner.pid, info["pid"])
        self.assertNotEqual(owner.pid, info["parent"])
        self.assertEqual(info["group"], info["parent"])
        self.assertEqual(info["session"], info["parent"])
        self.assertEqual(info["cwd"], str(self.root))
        self.assertEqual(info["value"], "private-environment")
        self.assertEqual(info["sockets"], [])
        self.assertEqual(await asyncio.wait_for(owner.stderr.readline(), 2), b"adapter-stderr\n")
        owner.stdin.write(b"dap-payload\n")
        await owner.stdin.drain()
        self.assertEqual(await asyncio.wait_for(owner.stdout.readline(), 2), b"echo:dap-payload\n")
        owner.stdin.write(b"exit\n")
        await owner.stdin.drain()
        self.assertEqual(await asyncio.wait_for(owner.wait(), 3), 17)
        self.assertEqual(owner.returncode, 17)
        self.assertEqual(await asyncio.wait_for(owner.stdout.read(), 2), b"")
        self.assertEqual(await asyncio.wait_for(owner.stderr.read(), 2), b"")
        result = await owner.cleanup(0.0, 2.0)
        self.assertTrue(result.complete)
        self.assertEqual(result.root_returncode, 17)

    async def test_root_exit_retains_guardian_for_ordinary_child_cleanup(self) -> None:
        owner, info = await self._launch("exit")
        self.assertEqual(await asyncio.wait_for(owner.wait(), 3), 23)
        self.assertEqual(await asyncio.wait_for(owner.stdout.read(), 2), b"")
        child = json.loads((self.root / "child.json").read_text())
        self.assertEqual(child["group"], info["group"])
        self.assertEqual(child["session"], info["session"])
        self.assertEqual(child["sockets"], [])
        before = (self.root / "pulse").read_text()
        await asyncio.sleep(0.08)
        self.assertNotEqual((self.root / "pulse").read_text(), before)
        result = await owner.cleanup(0.1, 2.0)
        self.assertTrue(result.complete)
        self.assertTrue(result.group_signal_sent)
        self.assertEqual(result.root_returncode, 23)
        self.assertEqual(result.guardian_returncode, -signal.SIGKILL)
        await self._pulse_stops()
        self.assertTrue((self.root / "child.term").exists())

    async def test_force_cleanup_and_cancelled_waiter_share_retained_owner(self) -> None:
        owner, _info = await self._launch("stubborn")
        first = asyncio.create_task(owner.cleanup(0.4, 2.0))
        await asyncio.sleep(0.05)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertIsNone(owner.returncode)
        second, third = await asyncio.gather(owner.cleanup(0.0, 2.0), owner.cleanup(0.0, 2.0))
        self.assertIs(second, third)
        self.assertIs(await owner.cleanup(0.0, 2.0), second)
        self.assertTrue(second.complete)
        self.assertEqual(second.root_returncode, -signal.SIGKILL)
        self.assertEqual(second.guardian_returncode, -signal.SIGKILL)
        await self._pulse_stops()

    async def test_cleanup_never_signals_from_server_and_preserves_other_owner(self) -> None:
        sentinel_directory = self.root / "sentinel"
        sentinel_directory.mkdir()
        sentinel, _info = await self._launch(directory=sentinel_directory)
        with (
            patch.object(owner_module.os, "kill", side_effect=AssertionError("server PID signal")),
            patch.object(
                owner_module.os, "killpg", side_effect=AssertionError("server group signal")
            ),
        ):
            owner, _info = await self._launch("tree")
            result = await owner.cleanup(0.1, 2.0)
            self.assertTrue(result.complete)
            await owner.aclose()
        self.assertIsNone(sentinel.returncode)
        sentinel.stdin.write(b"still-alive\n")
        await sentinel.stdin.drain()
        self.assertEqual(
            await asyncio.wait_for(sentinel.stdout.readline(), 2), b"echo:still-alive\n"
        )

    async def test_control_eof_cleans_live_and_already_exited_roots(self) -> None:
        for mode in ("tree", "exit"):
            with self.subTest(mode=mode):
                directory = self.root / mode
                directory.mkdir()
                environment = self._environment(mode, directory)
                environment["OWNER_ADAPTER"] = _ADAPTER
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
                controller = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-u",
                    "-c",
                    _CONTROLLER,
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _stdout, stderr = await asyncio.wait_for(controller.communicate(), 8)
                self.assertEqual(controller.returncode, 0, stderr.decode())
                self.assertTrue((directory / "root.json").exists())
                await self._pulse_stops(directory)
                self.assertTrue((directory / "child.term").exists())

    async def test_guardian_loss_does_not_invent_root_status_or_cleanup_success(self) -> None:
        owner, _info = await self._launch()
        owner.stdin.write(b"crash-group\n")
        await owner.stdin.drain()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(owner.wait(), 3)
        result = await owner.cleanup(0.0, 2.0)
        self.assertFalse(result.complete)
        self.assertFalse(result.group_signal_sent)
        self.assertIsNone(result.root_returncode)
        self.assertIsNone(owner.returncode)
        self.assertEqual(result.guardian_returncode, -signal.SIGKILL)

    async def test_adapter_launch_failure_reclaims_guardian_and_reports_os_error(self) -> None:
        spawned = []
        real_spawn = asyncio.create_subprocess_exec

        async def record_spawn(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            spawned.append(process)
            return process

        with patch.object(owner_module.asyncio, "create_subprocess_exec", record_spawn):
            with self.assertRaises(FileNotFoundError):
                await PosixOwnedProcess.launch(
                    generation=object(),
                    argv=[str(self.root / "missing-adapter")],
                    cwd=str(self.root),
                    env=None,
                )
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].returncode)

    async def test_cancelled_launch_keeps_spawn_custody_until_reclaimed(self) -> None:
        real_spawn = asyncio.create_subprocess_exec
        spawned = asyncio.Event()
        release = asyncio.Event()
        processes = []

        async def delayed_spawn(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            spawned.set()
            await release.wait()
            return process

        with patch.object(owner_module.asyncio, "create_subprocess_exec", delayed_spawn):
            launch = asyncio.create_task(
                PosixOwnedProcess.launch(
                    generation=object(),
                    argv=[sys.executable, "-u", "-c", _ADAPTER],
                    cwd=str(self.root),
                    env=self._environment("tree"),
                )
            )
            await asyncio.wait_for(spawned.wait(), 5)
            launch.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(launch, 8)
        self.assertIsNotNone(processes[0].returncode)
        if (self.root / "pulse").exists():
            await self._pulse_stops()

    async def test_devnull_stdin_and_natural_root_exit_keep_real_status(self) -> None:
        owner = await PosixOwnedProcess.launch(
            generation=object(),
            argv=[sys.executable, "-c", "raise SystemExit(31)"],
            cwd=None,
            env=None,
            stdin_mode="devnull",
        )
        self.owners.append(owner)
        self.assertIsNone(owner.stdin)
        self.assertEqual(await asyncio.wait_for(owner.wait(), 3), 31)
        self.assertEqual(await asyncio.wait_for(owner.stdout.read(), 2), b"")
        self.assertTrue((await owner.aclose()).complete)

    async def test_invalid_cleanup_deadlines_leave_live_owner_available(self) -> None:
        owner, _info = await self._launch()
        for invalid in (-1.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                await owner.cleanup(invalid, 2.0)
        self.assertIsNone(owner.returncode)
        self.assertTrue((await owner.cleanup(0.0, 2.0)).complete)

    async def test_private_helper_rejects_shared_session_before_adapter_admission(self) -> None:
        assert owner_module.__file__ is not None
        parent, child = socket.socketpair()
        try:
            helper = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(Path(owner_module.__file__).resolve()),
                "--guardian",
                str(child.fileno()),
                pass_fds=(child.fileno(),),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            child.close()
            # No launch configuration is sent. A broken guard can only wait for
            # it: closing this socket cannot trigger a signal before admission.
            try:
                code = await asyncio.wait_for(asyncio.shield(helper.wait()), 2)
                self.assertNotEqual(code, 0)
            finally:
                parent.close()
                await asyncio.wait_for(helper.wait(), 2)
        finally:
            parent.close()
            child.close()


if __name__ == "__main__":
    unittest.main()
