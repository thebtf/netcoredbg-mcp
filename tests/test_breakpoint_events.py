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

    def test_breakpoint_changed_clears_stale_dap_line(self, manager):
        """Adapter restoring the requested line must clear any stale dap_line."""
        bp = Breakpoint(file="test.cs", line=10, verified=True, id=42, dap_line=12)
        manager.breakpoints.add(bp)

        event = DAPEvent(seq=1, event="breakpoint", body={
            "reason": "changed",
            "breakpoint": {"id": 42, "verified": True, "line": 10},
        })
        manager._on_breakpoint(event)

        bps = manager.breakpoints.get_for_file("test.cs")
        assert bps[0].line == 10
        assert bps[0].dap_line is None

    def test_breakpoint_changed_propagates_clear_to_tracepoint(self, manager):
        """When _on_breakpoint clears bp.dap_line, tracepoint manager is updated too."""
        from netcoredbg_mcp.session.tracepoints import TracepointManager

        bp = Breakpoint(file="test.cs", line=10, verified=True, id=42, dap_line=12)
        manager.breakpoints.add(bp)
        mgr = TracepointManager()
        manager._tracepoint_manager = mgr
        tp = mgr.add("test.cs", 10, "x")
        tp.breakpoint_id = 42
        tp.dap_line = 12

        event = DAPEvent(seq=1, event="breakpoint", body={
            "reason": "changed",
            "breakpoint": {"id": 42, "verified": True, "line": 10},
        })
        manager._on_breakpoint(event)

        assert manager.breakpoints.get_for_file("test.cs")[0].dap_line is None
        assert tp.dap_line is None

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


class TestResolveHitCountKey:
    """Regression: runtime->requested line mapping for hit counts."""

    @pytest.fixture
    def manager(self):
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            from netcoredbg_mcp.session import SessionManager
            m = SessionManager()
            return m

    def test_resolves_dap_adjusted_line_back_to_requested(self, manager):
        """When DAP adjusted bp 10→12, stop at line 12 resolves to key (norm, 10)."""
        bp = Breakpoint(file="C:/src/test.cs", line=10, id=1, dap_line=12)
        manager.breakpoints.add(bp)
        norm = manager.breakpoints._normalize_path("C:/src/test.cs")

        key = manager._resolve_hit_count_key("C:/src/test.cs", 12)
        assert key == (norm, 10)

    def test_resolves_exact_requested_line(self, manager):
        """Non-adjusted bp: stop at bp.line resolves to bp.line."""
        bp = Breakpoint(file="C:/src/test.cs", line=10, id=1)
        manager.breakpoints.add(bp)
        norm = manager.breakpoints._normalize_path("C:/src/test.cs")

        key = manager._resolve_hit_count_key("C:/src/test.cs", 10)
        assert key == (norm, 10)

    def test_falls_back_to_runtime_line_when_no_match(self, manager):
        """No matching bp (e.g., exception stop): falls back to runtime line."""
        norm = manager.breakpoints._normalize_path("C:/src/test.cs")
        key = manager._resolve_hit_count_key("C:/src/test.cs", 99)
        assert key == (norm, 99)

    def test_update_and_read_share_the_same_key(self, manager):
        """_update_hit_count writes by requested line; get_stop_context reads by
        the same resolved key. DAP-adjusted bps must see the incremented count."""
        bp = Breakpoint(file="C:/src/test.cs", line=10, id=1, dap_line=12)
        manager.breakpoints.add(bp)
        norm = manager.breakpoints._normalize_path("C:/src/test.cs")

        # Simulate _update_hit_count writing by runtime line 12 -> resolved to 10
        write_key = manager._resolve_hit_count_key("C:/src/test.cs", 12)
        manager._state.hit_counts[write_key] = 5

        # get_stop_context reads by the same resolver against runtime line 12
        read_key = manager._resolve_hit_count_key("C:/src/test.cs", 12)
        assert manager._state.hit_counts.get(read_key) == 5
