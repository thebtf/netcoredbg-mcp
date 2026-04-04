"""Tests for client-side breakpoint hit counting."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.dap.protocol import DAPEvent
from netcoredbg_mcp.session.state import StackFrame


class TestHitCounting:
    """Tests for breakpoint hit counting via _update_hit_count."""

    @pytest.fixture
    def manager(self):
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            from netcoredbg_mcp.session import SessionManager
            m = SessionManager()
            return m

    @pytest.mark.asyncio
    async def test_hit_count_increments(self, manager):
        """Hit count increments when stopped at a breakpoint location."""
        manager.get_stack_trace = AsyncMock(return_value=[
            StackFrame(id=1, name="Main", source="C:\\app\\Program.cs", line=10, column=1),
        ])
        # Add a breakpoint at that location
        from netcoredbg_mcp.session.state import Breakpoint
        manager.breakpoints.add(Breakpoint(file="C:\\app\\Program.cs", line=10))

        await manager._update_hit_count(thread_id=1)

        norm_path = manager.breakpoints._normalize_path("C:\\app\\Program.cs")
        assert manager.state.hit_counts.get((norm_path, 10)) == 1

        # Second hit
        await manager._update_hit_count(thread_id=1)
        assert manager.state.hit_counts.get((norm_path, 10)) == 2

    @pytest.mark.asyncio
    async def test_hit_count_zero_for_unhit(self, manager):
        """Breakpoints that haven't been hit should have no entry in hit_counts."""
        assert manager.state.hit_counts == {}

    @pytest.mark.asyncio
    async def test_hit_count_no_frames(self, manager):
        """No crash when stack trace returns empty."""
        manager.get_stack_trace = AsyncMock(return_value=[])
        await manager._update_hit_count(thread_id=1)
        assert manager.state.hit_counts == {}

    @pytest.mark.asyncio
    async def test_hit_count_no_source(self, manager):
        """No crash when top frame has no source."""
        manager.get_stack_trace = AsyncMock(return_value=[
            StackFrame(id=1, name="[External]", source=None, line=0, column=0),
        ])
        await manager._update_hit_count(thread_id=1)
        assert manager.state.hit_counts == {}

    def test_on_stopped_captures_description(self, manager):
        """Stopped event captures description and text fields."""
        event = DAPEvent(
            seq=1,
            event="stopped",
            body={
                "reason": "breakpoint",
                "threadId": 1,
                "description": "Breakpoint hit",
                "text": "at Program.cs:10",
            },
        )
        manager._on_stopped(event)
        assert manager.state.stop_description == "Breakpoint hit"
        assert manager.state.stop_text == "at Program.cs:10"

    def test_on_stopped_none_description(self, manager):
        """Stopped event with no description fields stores None."""
        event = DAPEvent(
            seq=1,
            event="stopped",
            body={"reason": "step", "threadId": 1},
        )
        manager._on_stopped(event)
        assert manager.state.stop_description is None
        assert manager.state.stop_text is None
