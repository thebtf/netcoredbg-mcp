"""Tests for TracepointManager — tracepoints, rate limiting, buffer."""

from __future__ import annotations

import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.session.state import TraceEntry, Tracepoint
from netcoredbg_mcp.session.tracepoints import (
    MAX_TRACE_ENTRIES,
    RATE_LIMIT_INTERVAL_SECONDS,
    TracepointManager,
)


class TestTracepointAdd:

    def test_add_creates_tracepoint(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x + y")
        assert tp.id == "tp-1"
        assert tp.file == "test.cs"
        assert tp.line == 10
        assert tp.expression == "x + y"
        assert tp.active is True
        assert tp.hit_count == 0

    def test_add_increments_counter(self):
        mgr = TracepointManager()
        tp1 = mgr.add("a.cs", 1, "a")
        tp2 = mgr.add("b.cs", 2, "b")
        assert tp1.id == "tp-1"
        assert tp2.id == "tp-2"

    def test_remove_returns_tracepoint(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")
        removed = mgr.remove("tp-1")
        assert removed is tp
        assert "tp-1" not in mgr.tracepoints

    def test_remove_nonexistent_returns_none(self):
        mgr = TracepointManager()
        assert mgr.remove("tp-999") is None


class TestTracepointLog:

    def test_get_log_empty(self):
        mgr = TracepointManager()
        assert mgr.get_log() == []

    def test_get_log_returns_entries(self):
        mgr = TracepointManager()
        entry = TraceEntry(
            timestamp=1.0, file="test.cs", line=10, expression="x",
            value="42", thread_id=1, tracepoint_id="tp-1",
        )
        mgr._trace_buffer.append(entry)
        log = mgr.get_log()
        assert len(log) == 1
        assert log[0].value == "42"

    def test_get_log_filter_by_tracepoint(self):
        mgr = TracepointManager()
        mgr._trace_buffer.append(TraceEntry(1.0, "a.cs", 1, "a", "1", 1, "tp-1"))
        mgr._trace_buffer.append(TraceEntry(2.0, "b.cs", 2, "b", "2", 1, "tp-2"))
        mgr._trace_buffer.append(TraceEntry(3.0, "a.cs", 1, "a", "3", 1, "tp-1"))

        log = mgr.get_log(tracepoint_id="tp-1")
        assert len(log) == 2
        assert all(e.tracepoint_id == "tp-1" for e in log)

    def test_get_log_filter_since(self):
        mgr = TracepointManager()
        mgr._trace_buffer.append(TraceEntry(1.0, "a.cs", 1, "a", "1", 1, "tp-1"))
        mgr._trace_buffer.append(TraceEntry(5.0, "a.cs", 1, "a", "2", 1, "tp-1"))

        log = mgr.get_log(since=3.0)
        assert len(log) == 1
        assert log[0].value == "2"

    def test_clear_log(self):
        mgr = TracepointManager()
        mgr._trace_buffer.append(TraceEntry(1.0, "a.cs", 1, "a", "1", 1, "tp-1"))
        mgr._trace_buffer.append(TraceEntry(2.0, "a.cs", 1, "a", "2", 1, "tp-1"))
        count = mgr.clear_log()
        assert count == 2
        assert len(mgr._trace_buffer) == 0


class TestTracepointBufferFIFO:

    def test_buffer_evicts_oldest_at_limit(self):
        mgr = TracepointManager()
        # Fill buffer to max
        for i in range(MAX_TRACE_ENTRIES + 10):
            mgr._trace_buffer.append(
                TraceEntry(float(i), "a.cs", 1, "a", str(i), 1, "tp-1")
            )
        assert len(mgr._trace_buffer) == MAX_TRACE_ENTRIES
        # Oldest should be entry 10 (first 10 evicted)
        assert mgr._trace_buffer[0].value == "10"


class TestTracepointRateLimit:

    def test_rate_limited_when_too_fast(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")

        # First hit: not limited
        assert mgr._is_rate_limited(tp.id) is False
        # Immediate second hit: limited
        assert mgr._is_rate_limited(tp.id) is True

    def test_not_rate_limited_after_interval(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")

        mgr._is_rate_limited(tp.id)
        # Simulate time passing
        mgr._last_hit_times[tp.id] = time.monotonic() - RATE_LIMIT_INTERVAL_SECONDS - 0.01
        assert mgr._is_rate_limited(tp.id) is False


class TestTracepointFindLocation:

    def test_find_by_file_line(self):
        mgr = TracepointManager()
        tp = mgr.add("C:/src/test.cs", 10, "x")
        found = mgr.find_tracepoint_for_location("C:\\src\\test.cs", 10)
        assert found is tp

    def test_not_found_wrong_line(self):
        mgr = TracepointManager()
        mgr.add("test.cs", 10, "x")
        assert mgr.find_tracepoint_for_location("test.cs", 20) is None

    def test_not_found_inactive(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")
        tp.active = False
        assert mgr.find_tracepoint_for_location("test.cs", 10) is None
