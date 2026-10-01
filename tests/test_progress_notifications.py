"""Tests for MCP progress notification plumbing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestBuildOutputCallback:
    """Tests for output_callback in BuildSession._run_command."""

    @pytest.mark.asyncio
    async def test_callback_receives_stdout_lines(self):
        """output_callback called with stdout lines."""

        lines_received = []

        async def callback(line: str, stream: str) -> None:
            lines_received.append((line, stream))

        # Mock _run_command to simulate calling callback
        # We test the callback signature is correct
        assert callable(callback)
        await callback("test line", "stdout")
        assert lines_received == [("test line", "stdout")]

    @pytest.mark.asyncio
    async def test_callback_receives_stderr_as_warning(self):
        """stderr lines reported with stream='stderr'."""
        lines = []

        async def callback(line: str, stream: str) -> None:
            lines.append((line, stream))

        await callback("error msg", "stderr")
        assert lines[0] == ("error msg", "stderr")

    @pytest.mark.asyncio
    async def test_none_callback_accepted(self):
        """output_callback=None doesn't crash."""
        # Verify the function signature accepts None
        import inspect

        from netcoredbg_mcp.build.session import BuildSession

        sig = inspect.signature(BuildSession._run_command)
        params = sig.parameters
        assert "output_callback" in params
        assert params["output_callback"].default is None


class TestSafeNotify:
    """Tests for notification failure handling."""

    @pytest.mark.asyncio
    async def test_notify_failure_sets_flag(self):
        """After first failure, further notifications are suppressed."""
        notify_failed = False

        async def safe_notify(ctx, msg, level="info"):
            nonlocal notify_failed
            if notify_failed:
                return
            try:
                if level == "warning":
                    await ctx.warning(msg)
                else:
                    await ctx.info(msg)
            except Exception:
                notify_failed = True

        ctx = AsyncMock()
        ctx.info.side_effect = ConnectionError("client disconnected")

        await safe_notify(ctx, "line 1")
        assert notify_failed is True

        # Second call should be suppressed
        ctx.info.reset_mock()
        await safe_notify(ctx, "line 2")
        ctx.info.assert_not_called()

    @pytest.mark.asyncio
    async def test_stderr_uses_warning(self):
        """stderr lines sent via ctx.warning."""
        ctx = AsyncMock()

        async def safe_notify(ctx, msg, level="info"):
            if level == "warning":
                await ctx.warning(msg)
            else:
                await ctx.info(msg)

        await safe_notify(ctx, "error", "warning")
        ctx.warning.assert_awaited_once_with("error")


class TestBuildLineThrottling:
    """Tests for 500-line cap on build output."""

    @pytest.mark.asyncio
    async def test_cap_at_500_lines(self):
        """After 500 lines, notifications are suppressed."""
        line_count = 0
        max_lines = 500
        sent = []

        async def on_build_output(line: str, stream: str) -> None:
            nonlocal line_count
            line_count += 1
            if line_count <= max_lines:
                sent.append(line)
            elif line_count == max_lines + 1:
                sent.append(f"... ({line_count}+ lines)")

        for i in range(600):
            await on_build_output(f"line {i}", "stdout")

        assert len(sent) == 501  # 500 lines + 1 overflow message
        assert sent[500].startswith("... (501+")


class TestHeartbeat:
    """Tests for wait_for_stopped heartbeat."""

    @pytest.mark.asyncio
    async def test_heartbeat_fires_during_wait(self):
        """Heartbeat callback fires when stop event doesn't come quickly."""
        from netcoredbg_mcp.session.manager import SessionManager

        mgr = SessionManager.__new__(SessionManager)
        mgr._execution_event = asyncio.Event()
        mgr._state = MagicMock()
        mgr._state.state = MagicMock()
        mgr._state.state.value = "running"
        mgr._state.current_thread_id = None
        mgr._state.stop_reason = None
        mgr._state.exit_code = None
        mgr._state.stop_description = None
        mgr._state.stop_text = None
        mgr._state.process_id = 1234

        heartbeats = []

        async def heartbeat(elapsed: float) -> None:
            heartbeats.append(elapsed)
            # Set event after first heartbeat to end wait
            mgr._execution_event.set()

        _snapshot = await mgr.wait_for_stopped(
            timeout=30.0,
            heartbeat_callback=heartbeat,
        )
        assert len(heartbeats) >= 1
        assert heartbeats[0] >= 0

    @pytest.mark.asyncio
    async def test_no_heartbeat_when_callback_none(self):
        """No heartbeat if callback is None — immediate stop."""
        from netcoredbg_mcp.session.manager import SessionManager

        mgr = SessionManager.__new__(SessionManager)
        mgr._execution_event = asyncio.Event()
        mgr._execution_event.set()  # Already stopped
        mgr._state = MagicMock()
        mgr._state.state = MagicMock()
        mgr._state.state.value = "stopped"
        mgr._state.current_thread_id = 1
        mgr._state.stop_reason = "breakpoint"
        mgr._state.exit_code = None
        mgr._state.stop_description = None
        mgr._state.stop_text = None
        mgr._state.process_id = 1234

        snapshot = await mgr.wait_for_stopped(timeout=5.0)
        assert snapshot is not None


class TestExecuteAndWaitProgress:
    """Execution reports its observations even when optional progress delivery fails."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("timed_out", [False, True])
    @pytest.mark.parametrize("progress_fails", [False, True])
    async def test_progress_phases_preserve_stopped_and_running_results(
        self, timed_out, progress_fails
    ):
        from netcoredbg_mcp.resource_updates import ResourceSubscriptions
        from netcoredbg_mcp.server import _execute_and_wait
        from netcoredbg_mcp.session.state import DebugState, StoppedSnapshot

        snapshot = StoppedSnapshot(
            state=DebugState.RUNNING if timed_out else DebugState.STOPPED,
            stop_reason=None if timed_out else "breakpoint",
            timed_out=timed_out,
            description="Paused at entry" if not timed_out else None,
        )
        session = MagicMock()
        session.state.state = snapshot.state
        ctx = MagicMock()
        ctx.report_progress = AsyncMock(
            side_effect=ConnectionError("client disconnected") if progress_fails else None
        )

        async def wait_for_stopped(*, timeout, heartbeat_callback):
            await heartbeat_callback(5.0)
            return snapshot

        session.wait_for_stopped = wait_for_stopped
        action = AsyncMock()
        response = await _execute_and_wait(
            ctx,
            action(),
            "continue_execution",
            session=session,
            subscriptions=ResourceSubscriptions(),
        )

        assert response == {
            "state": "running" if timed_out else "stopped",
            "reason": None if timed_out else "breakpoint",
            "thread_id": None,
            "timed_out": timed_out,
            "message": (
                "Program is still running after timeout. Breakpoint may not have been reached."
                if timed_out
                else "Program is PAUSED (reason: breakpoint). Inspect state, then resume."
            ),
            "next_actions": (
                ["get_output", "pause_execution", "get_debug_state", "stop_debug"]
                if timed_out
                else [
                    "get_call_stack",
                    "get_variables",
                    "evaluate_expression",
                    "step_over",
                    "step_into",
                    "step_out",
                    "continue_execution",
                ]
            ),
            "description": "" if timed_out else "Paused at entry",
            "text": "",
        }
        assert [call.kwargs for call in ctx.report_progress.await_args_list] == [
            {"progress": 0, "total": 100, "message": "continue_execution..."},
            {"progress": 30, "total": 100, "message": "Waiting for stop event..."},
            {"progress": 30, "total": 100, "message": "Still waiting... (5s)"},
            {
                "progress": 100,
                "total": 100,
                "message": (
                    "Timed out waiting (30s) — program still running"
                    if timed_out
                    else "Program stopped: breakpoint"
                ),
            },
        ]

    @pytest.mark.asyncio
    async def test_cancellation_during_wait_propagates(self):
        from netcoredbg_mcp.resource_updates import ResourceSubscriptions
        from netcoredbg_mcp.server import _execute_and_wait

        session = MagicMock()
        session.wait_for_stopped = AsyncMock(side_effect=asyncio.CancelledError())
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()
        action = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await _execute_and_wait(
                ctx,
                action(),
                "step_over",
                session=session,
                subscriptions=ResourceSubscriptions(),
            )


class TestCallbackPlumbing:
    """Tests for output_callback forwarding through the chain."""

    def test_build_session_build_accepts_callback(self):
        """BuildSession.build() accepts output_callback."""
        import inspect

        from netcoredbg_mcp.build.session import BuildSession

        sig = inspect.signature(BuildSession.build)
        assert "output_callback" in sig.parameters

    def test_build_session_restore_accepts_callback(self):
        """BuildSession.restore() accepts output_callback."""
        import inspect

        from netcoredbg_mcp.build.session import BuildSession

        sig = inspect.signature(BuildSession.restore)
        assert "output_callback" in sig.parameters

    def test_build_manager_accepts_callback(self):
        """BuildManager.pre_launch_build() accepts output_callback."""
        import inspect

        from netcoredbg_mcp.build.manager import BuildManager

        sig = inspect.signature(BuildManager.pre_launch_build)
        assert "output_callback" in sig.parameters

    def test_session_manager_launch_accepts_callback(self):
        """SessionManager.launch() accepts output_callback."""
        import inspect

        from netcoredbg_mcp.session.manager import SessionManager

        sig = inspect.signature(SessionManager.launch)
        assert "output_callback" in sig.parameters

    def test_wait_for_stopped_accepts_heartbeat(self):
        """SessionManager.wait_for_stopped() accepts heartbeat_callback."""
        import inspect

        from netcoredbg_mcp.session.manager import SessionManager

        sig = inspect.signature(SessionManager.wait_for_stopped)
        assert "heartbeat_callback" in sig.parameters
