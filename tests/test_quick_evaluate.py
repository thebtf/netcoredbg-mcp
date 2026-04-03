"""Tests for quick_evaluate tool."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from netcoredbg_mcp.dap.protocol import DAPResponse
from netcoredbg_mcp.session.state import DebugState, StackFrame


class TestQuickEvaluate:

    @pytest.fixture
    def manager(self):
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            from netcoredbg_mcp.session import SessionManager
            m = SessionManager()
            m._state.state = DebugState.RUNNING
            m._state.current_thread_id = 1
            m._client.pause = AsyncMock(return_value=DAPResponse(
                seq=1, request_seq=1, success=True, command="pause"))
            m._client.evaluate = AsyncMock(return_value=DAPResponse(
                seq=2, request_seq=2, success=True, command="evaluate",
                body={"result": "42", "type": "int", "variablesReference": 0}))
            m._client.continue_execution = AsyncMock(return_value=DAPResponse(
                seq=3, request_seq=3, success=True, command="continue"))
            m.get_stack_trace = AsyncMock(return_value=[
                StackFrame(id=1, name="Main", source="test.cs", line=10, column=1)
            ])
            # Simulate stopped event firing after pause
            original_prepare = m.prepare_for_execution

            def mock_prepare():
                original_prepare()
                m._execution_event.set()
            m.prepare_for_execution = mock_prepare
            return m

    @pytest.mark.asyncio
    async def test_quick_evaluate_success(self, manager):
        """Quick evaluate returns result and resumes."""
        result = await manager.quick_evaluate("myVar")
        assert result["result"] == "42"
        assert result["type"] == "int"
        manager._client.pause.assert_called_once()
        manager._client.continue_execution.assert_called_once()

    @pytest.mark.asyncio
    async def test_quick_evaluate_not_running(self, manager):
        """Quick evaluate fails when program is not running."""
        manager._state.state = DebugState.STOPPED
        with pytest.raises(RuntimeError, match="not running"):
            await manager.quick_evaluate("x")

    @pytest.mark.asyncio
    async def test_quick_evaluate_eval_error(self, manager):
        """Quick evaluate returns error but still resumes."""
        manager._client.evaluate = AsyncMock(return_value=DAPResponse(
            seq=2, request_seq=2, success=False, command="evaluate",
            message="Variable not found"))
        result = await manager.quick_evaluate("badVar")
        assert "error" in result
        manager._client.continue_execution.assert_called_once()

    @pytest.mark.asyncio
    async def test_quick_evaluate_terminated(self, manager):
        """Quick evaluate fails when program is terminated."""
        manager._state.state = DebugState.TERMINATED
        with pytest.raises(RuntimeError, match="not running"):
            await manager.quick_evaluate("x")

    @pytest.mark.asyncio
    async def test_quick_evaluate_with_frame_id(self, manager):
        """Quick evaluate uses provided frame_id."""
        result = await manager.quick_evaluate("myVar", frame_id=5)
        assert result["result"] == "42"
        # Should not call get_stack_trace when frame_id provided
        manager.get_stack_trace.assert_not_called()
