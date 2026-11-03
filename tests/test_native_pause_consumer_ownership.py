"""Deterministic admission/cleanup regressions; no host, debugger or OS process runs.

The fake process table reproduces psutil's PID/PPID snapshot followed by fresh
Process construction. Race callbacks run between those two operations, without
sleeps. The consumer implementation must remain unchanged for the parent's RED.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from tests import native_pause_consumer as consumer

CLIENT_PID = 100
HOST_PID = 200
DEBUGGER_PID = 300
DEBUGGEE_PID = 400
FOREIGN_PID = 900


@dataclass
class _Row:
    birth: float
    parent: int
    executable: str
    arguments: list[str]
    alive: bool = True


class _Process:
    def __init__(self, table: _ProcessTable, pid: int) -> None:
        self.table = table
        self.pid = pid
        # Like a constructed psutil Process, this object retains its birth ID.
        self.birth = table.row(pid).birth

    def create_time(self) -> float:
        return self.birth

    def ppid(self) -> int:
        if self.pid in self.table.unreadable_parent:
            raise psutil.AccessDenied(self.pid)
        return self.table.row(self.pid).parent

    def parent(self) -> _Process | None:
        parent = self.ppid()
        return self.table.process(parent) if parent else None

    def status(self) -> str:
        self.table.row(self.pid)
        return psutil.STATUS_RUNNING

    def exe(self) -> str:
        return self.table.row(self.pid).executable

    def cmdline(self) -> list[str]:
        return list(self.table.row(self.pid).arguments)

    def is_running(self) -> bool:
        try:
            return self.table.row(self.pid).birth == self.birth
        except psutil.NoSuchProcess:
            return False

    def children(self, recursive: bool = False) -> list[_Process]:
        if not self.is_running():
            raise psutil.NoSuchProcess(self.pid)
        # Snapshot numeric parent links BEFORE constructing candidate objects.
        snapshot = {pid: row.parent for pid, row in self.table.rows.items() if row.alive}
        selected: list[int] = []
        frontier = [self.pid]
        visited = {self.pid}
        while frontier:
            parent = frontier.pop(0)
            direct = [
                pid for pid, ppid in snapshot.items() if ppid == parent and pid not in visited
            ]
            selected.extend(direct)
            visited.update(direct)
            if recursive:
                frontier.extend(direct)
        race = self.table.after_snapshot.pop(self.pid, None)
        if race is not None:
            self.table.races_fired.append(self.pid)
            race()
        result = []
        for pid in selected:
            try:
                result.append(self.table.process(pid))
            except psutil.NoSuchProcess:
                pass
        return result

    def kill(self) -> None:
        # Model the verified Windows backend boundary: the high-level identity
        # check precedes a backend operation that opens/kills the numeric PID.
        row = self.table.row(self.pid)
        if row.birth != self.birth:
            raise psutil.NoSuchProcess(self.pid)
        self.table.kill_attempts.append((self.pid, self.birth))
        race = self.table.after_kill_identity_check.pop(self.pid, None)
        if race is not None:
            self.table.kill_races_fired.append(self.pid)
            race()
        target = self.table.row(self.pid)
        self.table.killed.append((self.pid, target.birth))
        target.alive = False


class _ProcessTable:
    def __init__(self) -> None:
        self.rows: dict[int, _Row] = {}
        self.after_snapshot: dict[int, Callable[[], None]] = {}
        self.after_kill_identity_check: dict[int, Callable[[], None]] = {}
        self.kill_races_fired: list[int] = []
        self.kill_attempts: list[tuple[int, float]] = []
        self.races_fired: list[int] = []
        self.unreadable_parent: set[int] = set()
        self.killed: list[tuple[int, float]] = []

    def add(
        self,
        pid: int,
        birth: float,
        parent: int,
        executable: Path,
        arguments: list[str] | None = None,
    ) -> None:
        self.rows[pid] = _Row(birth, parent, str(executable), arguments or [str(executable)])

    def row(self, pid: int) -> _Row:
        row = self.rows.get(pid)
        if row is None or not row.alive:
            raise psutil.NoSuchProcess(pid)
        return row

    def process(self, pid: int | None = None) -> _Process:
        return _Process(self, CLIENT_PID if pid is None else pid)

    @staticmethod
    def wait_procs(
        processes: list[_Process], timeout: float
    ) -> tuple[list[_Process], list[_Process]]:
        # No wall-clock waiting or real subprocess is needed for cleanup proof.
        assert timeout >= 0
        return (
            [process for process in processes if not process.is_running()],
            [process for process in processes if process.is_running()],
        )


@dataclass
class _Harness:
    table: _ProcessTable
    owner: consumer.OwnedProcesses
    paths: dict[str, Path]
    events: list[dict[str, Any]]

    async def bind(self) -> None:
        self.table.add(HOST_PID, 1000.0, CLIENT_PID, self.paths["host"])
        self.owner.bind_host()
        # Bind through the public owner entry, but stop the periodic sampler;
        # each test advances capture explicitly at its deterministic race point.
        self.owner.finished.set()
        if self.owner.watcher is not None:
            await self.owner.watcher

    def add_debugger(self) -> None:
        self.table.add(DEBUGGER_PID, 1001.0, HOST_PID, self.paths["debugger"])

    def add_debuggee(self) -> None:
        self.table.add(
            DEBUGGEE_PID,
            1002.0,
            DEBUGGER_PID,
            self.paths["dotnet"],
            [str(self.paths["dotnet"]), str(self.paths["program"])],
        )

    async def reject_and_cleanup(self, foreign: set[tuple[int, float]]) -> None:
        self.owner.capture()
        await self.owner.cleanup()
        admitted = foreign.intersection(self.owner.processes)
        killed = foreign.intersection(self.table.killed)
        assert not admitted and not killed, (
            f"Unrelated process identities must never be owned or killed; "
            f"admitted={sorted(admitted)}, killed={sorted(killed)}"
        )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Harness:
    paths = {name: tmp_path / name for name in ("host", "debugger", "dotnet", "program", "foreign")}
    table = _ProcessTable()
    table.add(CLIENT_PID, 100.0, 0, paths["foreign"])
    table.add(FOREIGN_PID, 200.0, 0, paths["foreign"])
    events: list[dict[str, Any]] = []
    # Replace the entire module reference, not the process-global psutil module:
    # no accidental real Process/kill/global scan can occur through this seam.
    monkeypatch.setattr(
        consumer,
        "psutil",
        SimpleNamespace(
            Process=table.process,
            wait_procs=table.wait_procs,
            NoSuchProcess=psutil.NoSuchProcess,
            ZombieProcess=psutil.ZombieProcess,
            AccessDenied=psutil.AccessDenied,
            STATUS_ZOMBIE=psutil.STATUS_ZOMBIE,
        ),
    )
    monkeypatch.setattr(
        consumer, "time", SimpleNamespace(time=lambda: 1000.0, monotonic=time.monotonic)
    )
    monkeypatch.setattr(
        consumer, "emit", lambda event, **data: events.append({"event": event, **data})
    )
    owner = consumer.OwnedProcesses(paths["host"], paths["debugger"], paths["program"])
    return _Harness(table, owner, paths, events)


@pytest.mark.asyncio
async def test_capture_rejects_child_pid_reused_between_snapshot_and_adoption(
    harness: _Harness,
) -> None:
    await harness.bind()
    harness.add_debugger()

    def reuse_child() -> None:
        # Same executable intentionally prevents executable-name filtering from
        # disguising the missing ancestry proof.
        harness.table.add(DEBUGGER_PID, 1010.0, FOREIGN_PID, harness.paths["debugger"])

    harness.table.after_snapshot[HOST_PID] = reuse_child
    await harness.reject_and_cleanup({(DEBUGGER_PID, 1010.0)})
    assert harness.table.races_fired == [HOST_PID]


@pytest.mark.asyncio
async def test_capture_rejects_children_of_reused_pinned_parent_pid(harness: _Harness) -> None:
    await harness.bind()
    harness.add_debugger()

    def reuse_parent_and_child() -> None:
        # Numeric PPID still matches HOST_PID. Only revalidating the pinned
        # parent's birth after enumeration distinguishes this foreign tree.
        harness.table.add(HOST_PID, 1010.0, CLIENT_PID, harness.paths["host"])
        harness.table.add(DEBUGGER_PID, 1011.0, HOST_PID, harness.paths["debugger"])

    harness.table.after_snapshot[HOST_PID] = reuse_parent_and_child
    await harness.reject_and_cleanup({(HOST_PID, 1010.0), (DEBUGGER_PID, 1011.0)})
    assert harness.table.races_fired == [HOST_PID]


@pytest.mark.asyncio
async def test_capture_rejects_reused_intermediate_parent_and_nested_foreign_child(
    harness: _Harness,
) -> None:
    await harness.bind()
    harness.add_debugger()
    harness.add_debuggee()

    def reuse_intermediate() -> None:
        harness.table.add(DEBUGGER_PID, 1010.0, FOREIGN_PID, harness.paths["debugger"])
        harness.table.add(
            DEBUGGEE_PID,
            1011.0,
            DEBUGGER_PID,
            harness.paths["dotnet"],
            [str(harness.paths["dotnet"]), str(harness.paths["program"])],
        )

    harness.table.after_snapshot[HOST_PID] = reuse_intermediate
    await harness.reject_and_cleanup({(DEBUGGER_PID, 1010.0), (DEBUGGEE_PID, 1011.0)})
    assert harness.table.races_fired == [HOST_PID]


@pytest.mark.asyncio
async def test_capture_rejects_child_whose_ancestry_changed_after_snapshot(
    harness: _Harness,
) -> None:
    await harness.bind()
    harness.add_debugger()
    harness.add_debuggee()

    def move_child() -> None:
        # No PID/birth change: the fresh immediate parent link itself is needed.
        harness.table.rows[DEBUGGEE_PID].parent = FOREIGN_PID

    harness.table.after_snapshot[HOST_PID] = move_child
    await harness.reject_and_cleanup({(DEBUGGEE_PID, 1002.0)})
    assert harness.table.races_fired == [HOST_PID]
    assert (DEBUGGER_PID, 1001.0) in harness.owner.processes


@pytest.mark.asyncio
async def test_capture_does_not_own_or_kill_child_with_unreadable_parent_link(
    harness: _Harness,
) -> None:
    await harness.bind()
    harness.add_debugger()
    harness.table.unreadable_parent.add(DEBUGGER_PID)
    await harness.reject_and_cleanup({(DEBUGGER_PID, 1001.0)})
    # Failed proof must be reported rather than silently claiming this child
    # was absent or clean. Retain the consumer's existing structured emit seam.
    assert any(
        event.get("event") == "ownership_rejected" and event.get("pid") == DEBUGGER_PID
        for event in harness.events
    )


@pytest.mark.asyncio
async def test_capture_tracks_valid_descendants_and_reports_survivors_without_killing(
    harness: _Harness,
) -> None:
    await harness.bind()
    harness.add_debugger()
    harness.add_debuggee()
    harness.owner.capture()
    harness.owner.require_fixture_tree()
    expected = {(HOST_PID, 1000.0), (DEBUGGER_PID, 1001.0), (DEBUGGEE_PID, 1002.0)}
    assert set(harness.owner.processes) == expected
    cleanup = await harness.owner.cleanup()
    assert harness.table.kill_attempts == []
    assert harness.table.killed == []
    assert {(row["pid"], row["create_time"]) for row in cleanup["remaining"]} == expected
    # Nonempty remaining is the existing main() cleanup-failure gate. It must
    # be emitted, not replaced with a claim that surviving processes were clean.
    assert any(
        event.get("event") == "cleanup" and event.get("remaining") == cleanup["remaining"]
        for event in harness.events
    )


@pytest.mark.asyncio
async def test_cleanup_never_kills_pid_replacement_after_psutil_identity_check(
    harness: _Harness,
) -> None:
    await harness.bind()
    harness.add_debugger()
    harness.owner.capture()
    assert (DEBUGGER_PID, 1001.0) in harness.owner.processes

    def replace_after_identity_check() -> None:
        harness.table.add(DEBUGGER_PID, 1010.0, FOREIGN_PID, harness.paths["debugger"])

    harness.table.after_kill_identity_check[DEBUGGER_PID] = replace_after_identity_check
    cleanup = await harness.owner.cleanup()
    assert (DEBUGGER_PID, 1010.0) not in harness.table.killed, (
        "PID-based cleanup killed the foreign replacement after its identity check; "
        f"races_fired={harness.table.kill_races_fired}, killed={harness.table.killed}"
    )
    # The safe route never enters the unsafe backend seam at all. Its pinned
    # survivor remains a cleanup failure for the real owners to resolve.
    assert harness.table.kill_attempts == []
    assert {(row["pid"], row["create_time"]) for row in cleanup["remaining"]} == {
        (HOST_PID, 1000.0),
        (DEBUGGER_PID, 1001.0),
    }
