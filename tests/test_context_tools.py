"""Tests for get_exception_context and get_stop_context."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from netcoredbg_mcp.dap.protocol import DAPResponse
from netcoredbg_mcp.session.state import (
    DebugState,
    OutputEntry,
    StackFrame,
    Variable,
)


class TestExceptionContext:

    @pytest.fixture
    def manager(self):
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            from netcoredbg_mcp.session import SessionManager
            m = SessionManager()
            m._state.state = DebugState.STOPPED
            m._state.stop_reason = "exception"
            m._state.current_thread_id = 1
            m._state.current_frame_id = 1
            return m

    @pytest.mark.asyncio
    async def test_returns_exception_info(self, manager):
        manager.get_exception_info = AsyncMock(return_value={
            "exceptionId": "System.NullReferenceException",
            "description": "Object reference not set",
            "breakMode": "always",
        })
        manager.get_stack_trace = AsyncMock(return_value=[
            StackFrame(id=1, name="Main", source="Program.cs", line=42, column=1),
        ])
        manager.get_scopes = AsyncMock(return_value=[
            {"name": "Locals", "variablesReference": 100},
        ])
        manager.get_variables = AsyncMock(return_value=[
            Variable(name="x", value="null", type="object", variables_reference=0),
        ])
        manager.evaluate = AsyncMock(return_value={"error": "no inner"})

        result = await manager.get_exception_context()

        assert result["threadId"] == 1
        assert "exceptionId" in result["exception"]
        assert len(result["frames"]) == 1
        assert result["frames"][0]["name"] == "Main"
        assert "variables" in result["frames"][0]
        assert result["innerExceptions"] == []

    @pytest.mark.asyncio
    async def test_not_at_exception_raises(self, manager):
        manager._state.stop_reason = "breakpoint"
        with pytest.raises(RuntimeError, match="Not stopped at an exception"):
            await manager.get_exception_context()

    @pytest.mark.asyncio
    async def test_not_stopped_raises(self, manager):
        manager._state.state = DebugState.RUNNING
        with pytest.raises(RuntimeError, match="not stopped"):
            await manager.get_exception_context()

    @pytest.mark.asyncio
    async def test_inner_exceptions(self, manager):
        manager.get_exception_info = AsyncMock(return_value={"exceptionId": "Outer"})
        manager.get_stack_trace = AsyncMock(return_value=[])

        call_count = 0
        async def mock_evaluate(expr, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if "GetType" in expr and call_count <= 2:
                return {"result": "System.IO.IOException"}
            if "Message" in expr and call_count <= 3:
                return {"result": "File not found"}
            return {"error": "null"}

        manager.evaluate = mock_evaluate
        result = await manager.get_exception_context()
        assert len(result["innerExceptions"]) >= 1


class TestStopContext:

    @pytest.fixture
    def manager(self):
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            from netcoredbg_mcp.session import SessionManager
            m = SessionManager()
            m._state.state = DebugState.STOPPED
            m._state.stop_reason = "breakpoint"
            m._state.current_thread_id = 1
            m._state.stop_description = "Hit breakpoint"
            return m

    @pytest.mark.asyncio
    async def test_returns_context(self, manager):
        manager.get_stack_trace = AsyncMock(return_value=[
            StackFrame(id=1, name="DoWork", source="Service.cs", line=10, column=1),
            StackFrame(id=2, name="Main", source="Program.cs", line=5, column=1),
        ])
        manager.get_scopes = AsyncMock(return_value=[
            {"name": "Locals", "variablesReference": 100},
        ])
        manager.get_variables = AsyncMock(return_value=[
            Variable(name="count", value="42", type="int", variables_reference=0),
        ])
        manager._state.output_buffer.append(OutputEntry(text="Hello\n", category="stdout"))

        result = await manager.get_stop_context()

        assert result["reason"] == "breakpoint"
        assert result["description"] == "Hit breakpoint"
        assert len(result["frames"]) == 2
        assert result["frames"][0]["name"] == "DoWork"
        assert len(result["locals"]) == 1
        assert result["locals"][0]["name"] == "count"
        assert len(result["recentOutput"]) == 1
        assert result["hitCount"] == 0

    @pytest.mark.asyncio
    async def test_not_stopped_raises(self, manager):
        manager._state.state = DebugState.RUNNING
        with pytest.raises(RuntimeError, match="not stopped"):
            await manager.get_stop_context()

    @pytest.mark.asyncio
    async def test_with_hit_count(self, manager):
        manager.get_stack_trace = AsyncMock(return_value=[
            StackFrame(id=1, name="Loop", source="C:\\app\\test.cs", line=15, column=1),
        ])
        manager.get_scopes = AsyncMock(return_value=[])
        norm = manager.breakpoints._normalize_path("C:\\app\\test.cs")
        manager._state.hit_counts[(norm, 15)] = 7

        result = await manager.get_stop_context()
        assert result["hitCount"] == 7

    @pytest.mark.asyncio
    async def test_no_variables(self, manager):
        manager.get_stack_trace = AsyncMock(return_value=[
            StackFrame(id=1, name="Main", source="test.cs", line=1, column=1),
        ])
        result = await manager.get_stop_context(include_variables=False)
        assert "locals" not in result

    @pytest.mark.asyncio
    async def test_exception_stop_includes_exception_info(self, manager):
        manager._state.stop_reason = "exception"
        manager.get_stack_trace = AsyncMock(return_value=[])
        manager.get_exception_info = AsyncMock(return_value={
            "exceptionId": "System.Exception",
        })
        result = await manager.get_stop_context(include_variables=False, include_output_tail=0)
        assert "exception" in result
        assert result["exception"]["exceptionId"] == "System.Exception"
