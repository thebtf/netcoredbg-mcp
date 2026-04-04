"""Tests for SnapshotManager — create, diff, FIFO eviction."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.session.state import DebugState, Snapshot, SnapshotVar, StackFrame, Variable
from netcoredbg_mcp.session.snapshots import MAX_SNAPSHOTS, SnapshotManager


@pytest.fixture
def manager():
    """Create a mock SessionManager in STOPPED state."""
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        from netcoredbg_mcp.session import SessionManager
        m = SessionManager()
        m._state.state = DebugState.STOPPED
        m._state.current_thread_id = 1
        m._state.current_frame_id = 1
        m.get_stack_trace = AsyncMock(return_value=[
            StackFrame(id=1, name="Main", source="Program.cs", line=42, column=1),
        ])
        m.get_scopes = AsyncMock(return_value=[
            {"name": "Locals", "variablesReference": 100},
        ])
        m.get_variables = AsyncMock(return_value=[
            Variable(name="x", value="42", type="int", variables_reference=0),
            Variable(name="name", value="hello", type="string", variables_reference=0),
        ])
        return m


class TestSnapshotCreate:

    @pytest.mark.asyncio
    async def test_create_captures_variables(self, manager):
        mgr = SnapshotManager()
        snap = await mgr.create("test1", manager)
        assert snap.name == "test1"
        assert snap.frame_name == "Main"
        assert len(snap.variables) == 2
        assert snap.variables["x"].value == "42"
        assert snap.variables["name"].value == "hello"

    @pytest.mark.asyncio
    async def test_create_duplicate_name_raises(self, manager):
        mgr = SnapshotManager()
        await mgr.create("test1", manager)
        with pytest.raises(ValueError, match="already exists"):
            await mgr.create("test1", manager)

    @pytest.mark.asyncio
    async def test_create_not_stopped_raises(self, manager):
        manager._state.state = DebugState.RUNNING
        mgr = SnapshotManager()
        with pytest.raises(RuntimeError, match="not stopped"):
            await mgr.create("test1", manager)


class TestSnapshotDiff:

    @pytest.mark.asyncio
    async def test_diff_detects_changes(self, manager):
        mgr = SnapshotManager()
        await mgr.create("before", manager)

        # Change variable values for second snapshot
        manager.get_variables = AsyncMock(return_value=[
            Variable(name="x", value="100", type="int", variables_reference=0),
            Variable(name="name", value="hello", type="string", variables_reference=0),
            Variable(name="y", value="new", type="string", variables_reference=0),
        ])
        await mgr.create("after", manager)

        diff = mgr.diff("before", "after")
        assert diff["unchanged_count"] == 1  # name unchanged
        assert len(diff["changed"]) == 1  # x changed
        assert diff["changed"][0]["name"] == "x"
        assert diff["changed"][0]["old_value"] == "42"
        assert diff["changed"][0]["new_value"] == "100"
        assert len(diff["added"]) == 1  # y added
        assert diff["added"][0]["name"] == "y"

    @pytest.mark.asyncio
    async def test_diff_detects_removed(self, manager):
        mgr = SnapshotManager()
        await mgr.create("before", manager)

        manager.get_variables = AsyncMock(return_value=[
            Variable(name="x", value="42", type="int", variables_reference=0),
        ])
        await mgr.create("after", manager)

        diff = mgr.diff("before", "after")
        assert len(diff["removed"]) == 1
        assert diff["removed"][0]["name"] == "name"

    def test_diff_nonexistent_raises(self):
        mgr = SnapshotManager()
        with pytest.raises(KeyError):
            mgr.diff("a", "b")


class TestSnapshotFIFO:

    @pytest.mark.asyncio
    async def test_evicts_oldest_at_max(self, manager):
        mgr = SnapshotManager()
        for i in range(MAX_SNAPSHOTS + 5):
            await mgr.create(f"snap-{i}", manager)

        assert len(mgr.snapshots) == MAX_SNAPSHOTS
        # First 5 should be evicted
        assert "snap-0" not in mgr.snapshots
        assert "snap-4" not in mgr.snapshots
        assert f"snap-{MAX_SNAPSHOTS + 4}" in mgr.snapshots


class TestSnapshotList:

    @pytest.mark.asyncio
    async def test_list_returns_metadata(self, manager):
        mgr = SnapshotManager()
        await mgr.create("snap1", manager)
        result = mgr.list_snapshots()
        assert len(result) == 1
        assert result[0]["name"] == "snap1"
        assert result[0]["frame"] == "Main"
        assert result[0]["variable_count"] == 2
