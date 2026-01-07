"""Tests for session manager."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.dap import DAPEvent, DAPResponse
from netcoredbg_mcp.session import DebugState, SessionManager


class TestSessionManagerInit:
    """Tests for SessionManager initialization."""

    def test_init_default(self):
        """Test default initialization."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()

            assert manager.state.state == DebugState.IDLE
            assert not manager.is_active
            assert manager.project_path is None

    def test_init_with_path(self):
        """Test initialization with netcoredbg path."""
        with patch("netcoredbg_mcp.session.manager.DAPClient") as mock_client:
            manager = SessionManager(netcoredbg_path="/custom/path")

            mock_client.assert_called_once_with("/custom/path")

    def test_init_with_project_path(self):
        """Test initialization with project path."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path="/project/root")

            assert manager.project_path is not None
            # Project path should be absolute
            assert os.path.isabs(manager.project_path)


class TestPathValidation:
    """Tests for path validation."""

    def test_validate_path_within_project(self, tmp_path):
        """Test validate_path allows paths within project."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path=str(tmp_path))

            # Create a file inside project
            test_file = tmp_path / "test.cs"
            test_file.write_text("// test")

            result = manager.validate_path(str(test_file), must_exist=True)
            assert result == str(test_file.resolve())

    def test_validate_path_outside_project_raises(self, tmp_path):
        """Test validate_path rejects paths outside project."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path=str(tmp_path))

            with pytest.raises(ValueError, match="outside project scope"):
                # Attempt to access parent directory
                manager.validate_path(str(tmp_path.parent / "other.cs"))

    def test_validate_path_traversal_blocked(self, tmp_path):
        """Test validate_path blocks path traversal attempts."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path=str(tmp_path))

            with pytest.raises(ValueError, match="outside project scope"):
                manager.validate_path(str(tmp_path / ".." / "other.cs"))

    def test_validate_path_no_project_scope(self, tmp_path):
        """Test validate_path works without project scope."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()  # No project_path

            # Should work for any valid path when no scope set
            test_file = tmp_path / "test.cs"
            test_file.write_text("// test")

            result = manager.validate_path(str(test_file), must_exist=True)
            assert result == str(test_file.resolve())

    def test_validate_path_must_exist(self, tmp_path):
        """Test validate_path checks existence when required."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path=str(tmp_path))

            with pytest.raises(ValueError, match="does not exist"):
                manager.validate_path(str(tmp_path / "nonexistent.cs"), must_exist=True)

    def test_validate_program_valid(self, tmp_path):
        """Test validate_program accepts .dll and .exe files."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path=str(tmp_path))

            # Create test files
            dll_file = tmp_path / "test.dll"
            dll_file.write_bytes(b"")
            exe_file = tmp_path / "test.exe"
            exe_file.write_bytes(b"")

            assert manager.validate_program(str(dll_file)) == str(dll_file.resolve())
            assert manager.validate_program(str(exe_file)) == str(exe_file.resolve())

    def test_validate_program_invalid_extension(self, tmp_path):
        """Test validate_program rejects non-.NET files."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path=str(tmp_path))

            # Create test file with wrong extension
            txt_file = tmp_path / "test.txt"
            txt_file.write_text("not a .NET assembly")

            with pytest.raises(ValueError, match=r"must be \.NET assembly"):
                manager.validate_program(str(txt_file))


class TestSessionManagerState:
    """Tests for session state management."""

    def test_is_active_idle(self):
        """Test is_active when idle."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()

            assert not manager.is_active

    def test_is_active_running(self):
        """Test is_active when running."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            manager._state.state = DebugState.RUNNING

            assert manager.is_active

    def test_is_active_stopped(self):
        """Test is_active when stopped."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            manager._state.state = DebugState.STOPPED

            assert manager.is_active

    def test_is_active_terminated(self):
        """Test is_active when terminated."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            manager._state.state = DebugState.TERMINATED

            assert not manager.is_active

    def test_state_change_listener(self):
        """Test state change listener is called."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            listener = MagicMock()
            manager.on_state_change(listener)

            manager._set_state(DebugState.RUNNING)

            listener.assert_called_once_with(DebugState.RUNNING)

    def test_state_change_listener_not_called_for_same_state(self):
        """Test listener not called when state doesn't change."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            listener = MagicMock()
            manager.on_state_change(listener)

            manager._set_state(DebugState.IDLE)  # Same as initial

            listener.assert_not_called()


class TestBreakpointOperations:
    """Tests for breakpoint operations."""

    @pytest.mark.asyncio
    async def test_add_breakpoint_when_inactive(self):
        """Test adding breakpoint when session is inactive."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()

            bp = await manager.add_breakpoint("test.cs", 10)

            assert bp.file == "test.cs"
            assert bp.line == 10
            assert len(manager.breakpoints.get_for_file("test.cs")) == 1

    @pytest.mark.asyncio
    async def test_add_conditional_breakpoint(self):
        """Test adding conditional breakpoint."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()

            bp = await manager.add_breakpoint("test.cs", 10, condition="x > 5")

            assert bp.condition == "x > 5"

    @pytest.mark.asyncio
    async def test_remove_breakpoint(self):
        """Test removing breakpoint."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            await manager.add_breakpoint("test.cs", 10)

            removed = await manager.remove_breakpoint("test.cs", 10)

            assert removed is True
            assert len(manager.breakpoints.get_for_file("test.cs")) == 0

    @pytest.mark.asyncio
    async def test_clear_breakpoints(self):
        """Test clearing all breakpoints."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            await manager.add_breakpoint("file1.cs", 10)
            await manager.add_breakpoint("file2.cs", 20)

            count = await manager.clear_breakpoints()

            assert count == 2


class TestEventHandlers:
    """Tests for DAP event handlers."""

    def test_on_stopped_event(self):
        """Test handling stopped event."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            manager._state.state = DebugState.RUNNING

            event = DAPEvent(
                seq=1,
                event="stopped",
                body={"reason": "breakpoint", "threadId": 1, "allThreadsStopped": True},
            )
            manager._on_stopped(event)

            assert manager.state.state == DebugState.STOPPED
            assert manager.state.current_thread_id == 1
            assert manager.state.stop_reason == "breakpoint"

    def test_on_continued_event(self):
        """Test handling continued event."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            manager._state.state = DebugState.STOPPED

            event = DAPEvent(seq=1, event="continued", body={})
            manager._on_continued(event)

            assert manager.state.state == DebugState.RUNNING

    def test_on_terminated_event(self):
        """Test handling terminated event."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            manager._state.state = DebugState.RUNNING

            event = DAPEvent(seq=1, event="terminated", body={})
            manager._on_terminated(event)

            assert manager.state.state == DebugState.TERMINATED

    def test_on_exited_event(self):
        """Test handling exited event."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()

            event = DAPEvent(seq=1, event="exited", body={"exitCode": 0})
            manager._on_exited(event)

            assert manager.state.exit_code == 0

    def test_on_output_event(self):
        """Test handling output event."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()

            event = DAPEvent(
                seq=1,
                event="output",
                body={"category": "stdout", "output": "Hello\n"},
            )
            manager._on_output(event)

            assert "Hello\n" in manager.state.output_buffer

    def test_output_buffer_limit(self):
        """Test output buffer doesn't grow unbounded (byte-based limit)."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()

            # Add data exceeding MAX_OUTPUT_BYTES (10MB)
            # Use ~1KB entries to reach limit faster
            large_output = "x" * 1000 + "\n"
            for i in range(15000):  # 15000 entries x ~1KB = ~15MB should trigger limit
                event = DAPEvent(
                    seq=i,
                    event="output",
                    body={"category": "stdout", "output": large_output},
                )
                manager._on_output(event)

            # Buffer should be trimmed (10MB / ~1KB per entry = ~10000 entries max)
            total_bytes = sum(len(s) for s in manager.state.output_buffer)
            assert total_bytes <= 10_000_000  # MAX_OUTPUT_BYTES


class TestInspection:
    """Tests for inspection operations."""

    @pytest.mark.asyncio
    async def test_get_threads(self):
        """Test getting threads."""
        with patch("netcoredbg_mcp.session.manager.DAPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.threads = AsyncMock(
                return_value=DAPResponse(
                    seq=1,
                    request_seq=1,
                    success=True,
                    command="threads",
                    body={"threads": [{"id": 1, "name": "Main"}]},
                )
            )
            mock_client_class.return_value = mock_client

            manager = SessionManager()
            manager._client = mock_client

            threads = await manager.get_threads()

            assert len(threads) == 1
            assert threads[0].id == 1
            assert threads[0].name == "Main"

    @pytest.mark.asyncio
    async def test_get_stack_trace(self):
        """Test getting stack trace."""
        with patch("netcoredbg_mcp.session.manager.DAPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.stack_trace = AsyncMock(
                return_value=DAPResponse(
                    seq=1,
                    request_seq=1,
                    success=True,
                    command="stackTrace",
                    body={
                        "stackFrames": [
                            {
                                "id": 0,
                                "name": "Main",
                                "source": {"path": "test.cs"},
                                "line": 10,
                                "column": 1,
                            }
                        ]
                    },
                )
            )
            mock_client_class.return_value = mock_client

            manager = SessionManager()
            manager._client = mock_client
            manager._state.current_thread_id = 1

            frames = await manager.get_stack_trace()

            assert len(frames) == 1
            assert frames[0].name == "Main"
            assert frames[0].line == 10

    @pytest.mark.asyncio
    async def test_get_variables(self, sample_variables):
        """Test getting variables."""
        with patch("netcoredbg_mcp.session.manager.DAPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.variables = AsyncMock(
                return_value=DAPResponse(
                    seq=1,
                    request_seq=1,
                    success=True,
                    command="variables",
                    body={"variables": sample_variables},
                )
            )
            mock_client_class.return_value = mock_client

            manager = SessionManager()
            manager._client = mock_client

            variables = await manager.get_variables(1)

            assert len(variables) == 2
            assert variables[0].name == "x"
            assert variables[0].value == "10"
            assert variables[1].name == "args"

    @pytest.mark.asyncio
    async def test_evaluate_success(self):
        """Test successful expression evaluation."""
        with patch("netcoredbg_mcp.session.manager.DAPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.evaluate = AsyncMock(
                return_value=DAPResponse(
                    seq=1,
                    request_seq=1,
                    success=True,
                    command="evaluate",
                    body={"result": "30", "type": "int", "variablesReference": 0},
                )
            )
            mock_client_class.return_value = mock_client

            manager = SessionManager()
            manager._client = mock_client

            result = await manager.evaluate("x + y")

            assert result["result"] == "30"
            assert result["type"] == "int"

    @pytest.mark.asyncio
    async def test_evaluate_failure(self):
        """Test failed expression evaluation."""
        with patch("netcoredbg_mcp.session.manager.DAPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.evaluate = AsyncMock(
                return_value=DAPResponse(
                    seq=1,
                    request_seq=1,
                    success=False,
                    command="evaluate",
                    message="Unknown identifier 'foo'",
                )
            )
            mock_client_class.return_value = mock_client

            manager = SessionManager()
            manager._client = mock_client

            result = await manager.evaluate("foo")

            assert "error" in result


class TestExecutionControl:
    """Tests for execution control operations."""

    @pytest.mark.asyncio
    async def test_continue_execution(self):
        """Test continue execution."""
        with patch("netcoredbg_mcp.session.manager.DAPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.continue_execution = AsyncMock(
                return_value=DAPResponse(
                    seq=1, request_seq=1, success=True, command="continue"
                )
            )
            mock_client_class.return_value = mock_client

            manager = SessionManager()
            manager._client = mock_client
            manager._state.current_thread_id = 1
            manager._state.state = DebugState.STOPPED

            result = await manager.continue_execution()

            assert result["success"] is True
            assert manager.state.state == DebugState.RUNNING

    @pytest.mark.asyncio
    async def test_continue_no_thread_raises(self):
        """Test continue without thread raises error."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            manager._state.current_thread_id = None

            with pytest.raises(RuntimeError, match="No thread"):
                await manager.continue_execution()

    @pytest.mark.asyncio
    async def test_step_over(self):
        """Test step over."""
        with patch("netcoredbg_mcp.session.manager.DAPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.step_over = AsyncMock(
                return_value=DAPResponse(
                    seq=1, request_seq=1, success=True, command="next"
                )
            )
            mock_client_class.return_value = mock_client

            manager = SessionManager()
            manager._client = mock_client
            manager._state.current_thread_id = 1

            result = await manager.step_over()

            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_step_in(self):
        """Test step into."""
        with patch("netcoredbg_mcp.session.manager.DAPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.step_in = AsyncMock(
                return_value=DAPResponse(
                    seq=1, request_seq=1, success=True, command="stepIn"
                )
            )
            mock_client_class.return_value = mock_client

            manager = SessionManager()
            manager._client = mock_client
            manager._state.current_thread_id = 1

            result = await manager.step_in()

            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_step_out(self):
        """Test step out."""
        with patch("netcoredbg_mcp.session.manager.DAPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.step_out = AsyncMock(
                return_value=DAPResponse(
                    seq=1, request_seq=1, success=True, command="stepOut"
                )
            )
            mock_client_class.return_value = mock_client

            manager = SessionManager()
            manager._client = mock_client
            manager._state.current_thread_id = 1

            result = await manager.step_out()

            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_pause(self):
        """Test pause execution."""
        with patch("netcoredbg_mcp.session.manager.DAPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.pause = AsyncMock(
                return_value=DAPResponse(
                    seq=1, request_seq=1, success=True, command="pause"
                )
            )
            mock_client_class.return_value = mock_client

            manager = SessionManager()
            manager._client = mock_client
            manager._state.current_thread_id = 1

            result = await manager.pause()

            assert result["success"] is True
