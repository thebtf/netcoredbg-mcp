"""Tests for dynamic breakpoint event handling."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from netcoredbg_mcp.dap.protocol import DAPEvent
from netcoredbg_mcp.session.state import Breakpoint


class TestBreakpointEvents:
    """Tests for _on_breakpoint event handler."""

    @pytest.fixture
    def manager(self):
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            from netcoredbg_mcp.session import SessionManager
            m = SessionManager()
            return m

    def test_breakpoint_changed_updates_verified(self, manager):
        """Changed event updates breakpoint verified status."""
        bp = Breakpoint(file="test.cs", line=10, verified=False, id=42)
        manager.breakpoints.add(bp)

        event = DAPEvent(seq=1, event="breakpoint", body={
            "reason": "changed",
            "breakpoint": {"id": 42, "verified": True, "line": 10},
        })
        manager._on_breakpoint(event)

        bps = manager.breakpoints.get_for_file("test.cs")
        assert len(bps) == 1
        assert bps[0].verified is True

    def test_breakpoint_changed_updates_line(self, manager):
        """Changed event updates breakpoint line when debugger adjusts."""
        bp = Breakpoint(file="test.cs", line=10, verified=False, id=42)
        manager.breakpoints.add(bp)

        event = DAPEvent(seq=1, event="breakpoint", body={
            "reason": "changed",
            "breakpoint": {"id": 42, "verified": True, "line": 12},
        })
        manager._on_breakpoint(event)

        bps = manager.breakpoints.get_for_file("test.cs")
        assert bps[0].line == 10
        assert bps[0].dap_line == 12

    def test_breakpoint_removed(self, manager):
        """Removed event removes breakpoint from registry."""
        bp = Breakpoint(file="test.cs", line=10, verified=True, id=42)
        manager.breakpoints.add(bp)

        event = DAPEvent(seq=1, event="breakpoint", body={
            "reason": "removed",
            "breakpoint": {"id": 42},
        })
        manager._on_breakpoint(event)

        bps = manager.breakpoints.get_for_file("test.cs")
        assert len(bps) == 0

    def test_breakpoint_unknown_id_ignored(self, manager):
        """Changed event for unknown breakpoint ID is ignored."""
        event = DAPEvent(seq=1, event="breakpoint", body={
            "reason": "changed",
            "breakpoint": {"id": 999, "verified": True},
        })
        # Should not raise
        manager._on_breakpoint(event)

    def test_breakpoint_new_unknown_logged(self, manager):
        """New event for unknown breakpoint logs but doesn't crash."""
        event = DAPEvent(seq=1, event="breakpoint", body={
            "reason": "new",
            "breakpoint": {"id": 100, "verified": True, "line": 5},
        })
        # Should not raise
        manager._on_breakpoint(event)
