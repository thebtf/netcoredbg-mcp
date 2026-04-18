"""Tests for TracepointManager — tracepoints, rate limiting, buffer."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.session.state import TraceEntry
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


def _make_session(evaluate_result: str = "42", *, success: bool = True) -> MagicMock:
    """Build a minimal mock SessionManager for on_tracepoint_hit tests."""
    session = MagicMock()

    response = MagicMock()
    response.success = success
    response.body = {"result": evaluate_result}
    response.message = None

    session._client.evaluate = AsyncMock(return_value=response)
    session._client.continue_execution = AsyncMock()
    session.prepare_for_execution = MagicMock()
    session.get_stack_trace = AsyncMock(return_value=[MagicMock(id=10)])
    return session


class TestOnTracepointHit:

    @pytest.mark.asyncio
    async def test_hit_logs_evaluation_result(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x + y")
        session = _make_session("99")

        await mgr.on_tracepoint_hit(tp, session, thread_id=1)

        assert tp.hit_count == 1
        log = mgr.get_log()
        assert len(log) == 1
        assert log[0].value == "99"
        assert log[0].tracepoint_id == tp.id

    @pytest.mark.asyncio
    async def test_hit_resumes_when_no_user_breakpoint(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")
        session = _make_session()

        await mgr.on_tracepoint_hit(tp, session, thread_id=1)

        session.prepare_for_execution.assert_called_once()
        session._client.continue_execution.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_hit_does_not_resume_with_user_breakpoint(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")
        session = _make_session()

        await mgr.on_tracepoint_hit(tp, session, thread_id=1, has_user_breakpoint=True)

        session._client.continue_execution.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hit_uses_provided_top_frame(self):
        """Verifies that top_frame avoids a redundant get_stack_trace call."""
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")
        session = _make_session()

        top_frame = MagicMock(id=99)
        await mgr.on_tracepoint_hit(tp, session, thread_id=1, top_frame=top_frame)

        # get_stack_trace must NOT have been called since top_frame was supplied
        session.get_stack_trace.assert_not_awaited()
        # evaluate must have used the supplied frame id
        session._client.evaluate.assert_awaited_once_with(tp.expression, 99)

    @pytest.mark.asyncio
    async def test_rate_limited_hit_logs_placeholder(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")
        session = _make_session()

        # First hit consumes the rate-limit slot
        await mgr.on_tracepoint_hit(tp, session, thread_id=1)
        # Immediate second hit must be rate-limited
        await mgr.on_tracepoint_hit(tp, session, thread_id=1)

        log = mgr.get_log()
        assert any(e.value == "<rate limited>" for e in log)

    @pytest.mark.asyncio
    async def test_evaluation_failure_logs_error(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "bad_expr")
        session = _make_session(success=False)
        session._client.evaluate.return_value.message = "undefined variable"

        await mgr.on_tracepoint_hit(tp, session, thread_id=1)

        log = mgr.get_log()
        assert log[0].value.startswith("<error:")


class TestTracepointDapLine:

    def test_find_by_dap_line(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")
        assert mgr.set_dap_line("tp-1", 11) is True
        assert mgr.find_tracepoint_for_location("test.cs", 10) is tp
        assert mgr.find_tracepoint_for_location("test.cs", 11) is tp

    def test_find_by_dap_line_filename_fallback(self):
        mgr = TracepointManager()
        tp = mgr.add("C:/src/test.cs", 10, "x")
        mgr.set_dap_line("tp-1", 11)
        assert mgr.find_tracepoint_for_location("D:\\otherdir\\test.cs", 11) is tp

    def test_set_dap_line_unknown_tp(self):
        mgr = TracepointManager()
        assert mgr.set_dap_line("tp-999", 42) is False

    def test_set_dap_line_clears_value(self):
        mgr = TracepointManager()
        mgr.add("test.cs", 10, "x")
        mgr.set_dap_line("tp-1", 11)
        assert mgr.tracepoints["tp-1"].dap_line == 11
        mgr.set_dap_line("tp-1", None)
        assert mgr.tracepoints["tp-1"].dap_line is None

    def test_set_dap_line_for_breakpoint(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")
        tp.breakpoint_id = 42
        mgr.set_dap_line_for_breakpoint(42, 11)
        assert tp.dap_line == 11

    def test_set_dap_line_for_breakpoint_not_found(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")
        tp.breakpoint_id = 42
        mgr.set_dap_line_for_breakpoint(999, 11)
        assert tp.dap_line is None

    def test_find_does_not_match_inactive_by_dap_line(self):
        mgr = TracepointManager()
        tp = mgr.add("test.cs", 10, "x")
        mgr.set_dap_line("tp-1", 11)
        tp.active = False
        assert mgr.find_tracepoint_for_location("test.cs", 10) is None
        assert mgr.find_tracepoint_for_location("test.cs", 11) is None
