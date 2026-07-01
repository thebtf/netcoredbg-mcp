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
            SessionManager(netcoredbg_path="/custom/path")

            mock_client.assert_called_once_with("/custom/path")

    def test_init_with_project_path(self):
        """Test initialization with project path."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path="/project/root")

            assert manager.project_path is not None
            # Project path should be absolute
            assert os.path.isabs(manager.project_path)


class TestHotReloadLaunch:
    @pytest.mark.asyncio
    async def test_launch_enables_hot_reload_before_debuggee_launch(self, tmp_path):
        netcoredbg = tmp_path / "netcoredbg.exe"
        ncdbhook = tmp_path / "ncdbhook.dll"
        program = tmp_path / "App.dll"
        netcoredbg.write_text("exe")
        ncdbhook.write_text("hook")
        program.write_bytes(b"")

        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(
                netcoredbg_path=str(netcoredbg),
                project_path=str(tmp_path),
            )
        fake_client = FakeLaunchClient(str(netcoredbg))
        manager._client = fake_client
        manager._initialized_event.set()

        with patch(
            "netcoredbg_mcp.session.manager.detect_enc_support",
            return_value={
                "supported": True,
                "ncdbhook_path": str(ncdbhook),
                "error": None,
            },
        ):
            result = await manager.launch(program=str(program), pre_build=False)

        assert result["success"] is True
        assert fake_client.events == [
            "set_hot_reload",
            "set_exception_breakpoints",
            "launch",
            "configuration_done",
        ]
        assert fake_client.hot_reload_enabled is True


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

    def test_validate_path_for_project_uses_supplied_worktree_scope(self, tmp_path):
        """validate_path_for_project uses supplied project worktrees, not session scope."""
        owner_project = tmp_path / "owner"
        observer_project = tmp_path / "observer"
        observer_worktree = tmp_path / "observer-wt"
        owner_project.mkdir()
        observer_project.mkdir()
        observer_worktree.mkdir()
        (observer_worktree / ".git").mkdir()
        plan_file = observer_worktree / "runtime-smoke-plan.json"
        plan_file.write_text("{}", encoding="utf-8")

        worktrees_dir = observer_project / ".git" / "worktrees" / "observer-wt"
        worktrees_dir.mkdir(parents=True)
        (worktrees_dir / "gitdir").write_text(
            str(observer_worktree / ".git"),
            encoding="utf-8",
        )

        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path=str(owner_project))

            with pytest.raises(ValueError, match="outside project scope"):
                manager.validate_path(str(plan_file))

            assert (
                manager.validate_path_for_project(
                    str(plan_file),
                    str(observer_project),
                )
                == str(plan_file.resolve())
            )
            assert manager.project_path == str(owner_project.resolve())

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

    def test_validate_program_exe_to_dll_resolution_net6(self, tmp_path):
        """Test validate_program resolves .exe to .dll for .NET 6+ apps.

        .NET 6+ WPF/WinForms apps create both App.exe (native host) and App.dll
        (managed code). Debugging the .exe causes deps.json conflicts, so we
        should auto-resolve to the .dll.
        """
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path=str(tmp_path))

            # Create .NET 6+ style output (exe + dll + runtimeconfig.json)
            exe_file = tmp_path / "MyApp.exe"
            dll_file = tmp_path / "MyApp.dll"
            runtimeconfig_file = tmp_path / "MyApp.runtimeconfig.json"

            exe_file.write_bytes(b"")
            dll_file.write_bytes(b"")
            runtimeconfig_file.write_text('{"runtimeOptions":{}}')

            # When given .exe, should resolve to .dll
            result = manager.validate_program(str(exe_file))
            assert result == str(dll_file.resolve())

    def test_validate_program_exe_no_resolution_without_runtimeconfig(self, tmp_path):
        """Test validate_program does NOT resolve .exe if no runtimeconfig.json.

        Without runtimeconfig.json, this is likely a .NET Framework app where
        .exe is the correct target.
        """
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path=str(tmp_path))

            # Create exe + dll but NO runtimeconfig.json
            exe_file = tmp_path / "LegacyApp.exe"
            dll_file = tmp_path / "LegacyApp.dll"

            exe_file.write_bytes(b"")
            dll_file.write_bytes(b"")

            # Should keep .exe (no auto-resolution)
            result = manager.validate_program(str(exe_file))
            assert result == str(exe_file.resolve())

    def test_validate_program_exe_no_resolution_without_matching_dll(self, tmp_path):
        """Test validate_program keeps .exe if no matching .dll exists."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path=str(tmp_path))

            # Create only .exe (no .dll)
            exe_file = tmp_path / "StandaloneApp.exe"
            runtimeconfig_file = tmp_path / "StandaloneApp.runtimeconfig.json"

            exe_file.write_bytes(b"")
            runtimeconfig_file.write_text('{"runtimeOptions":{}}')

            # Should keep .exe (no dll to resolve to)
            result = manager.validate_program(str(exe_file))
            assert result == str(exe_file.resolve())

    def test_validate_program_dll_no_resolution(self, tmp_path):
        """Test validate_program does not change .dll paths."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager(project_path=str(tmp_path))

            # Create .NET 6+ style output
            exe_file = tmp_path / "MyApp.exe"
            dll_file = tmp_path / "MyApp.dll"
            runtimeconfig_file = tmp_path / "MyApp.runtimeconfig.json"

            exe_file.write_bytes(b"")
            dll_file.write_bytes(b"")
            runtimeconfig_file.write_text('{"runtimeOptions":{}}')

            # When given .dll directly, should return .dll unchanged
            result = manager.validate_program(str(dll_file))
            assert result == str(dll_file.resolve())


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

    @pytest.mark.asyncio
    async def test_clear_breakpoints_rolls_back_function_breakpoints_on_sync_failure(self):
        """Test clearing function breakpoints rolls back if DAP sync fails."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()
            manager._state.state = DebugState.STOPPED
            manager._client.capabilities = {"supportsFunctionBreakpoints": True}
            manager._client.set_function_breakpoints = AsyncMock(
                return_value=DAPResponse(
                    seq=1,
                    request_seq=1,
                    success=True,
                    command="setFunctionBreakpoints",
                    body={"breakpoints": [{"verified": True, "id": 123}]},
                )
            )
            await manager.add_function_breakpoint("Foo.Bar")
            manager._client.set_function_breakpoints = AsyncMock(
                side_effect=RuntimeError("sync failed")
            )

            with pytest.raises(RuntimeError, match="sync failed"):
                await manager.clear_breakpoints()

            function_breakpoints = manager.breakpoints.get_function_breakpoints()
            assert len(function_breakpoints) == 1
            assert function_breakpoints[0].name == "Foo.Bar"


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

            assert any(e.text == "Hello\n" for e in manager.state.output_buffer)

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
            total_bytes = sum(len(e.text) for e in manager.state.output_buffer)
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
                return_value=DAPResponse(seq=1, request_seq=1, success=True, command="continue")
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
                return_value=DAPResponse(seq=1, request_seq=1, success=True, command="next")
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
                return_value=DAPResponse(seq=1, request_seq=1, success=True, command="stepIn")
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
                return_value=DAPResponse(seq=1, request_seq=1, success=True, command="stepOut")
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
                return_value=DAPResponse(seq=1, request_seq=1, success=True, command="pause")
            )
            mock_client_class.return_value = mock_client

            manager = SessionManager()
            manager._client = mock_client
            manager._state.current_thread_id = 1

            result = await manager.pause()

            assert result["success"] is True


class FakeLaunchClient:
    def __init__(self, netcoredbg_path: str = "netcoredbg.exe") -> None:
        self.netcoredbg_path = netcoredbg_path
        self.is_running = True
        self.capabilities: dict[str, object] = {}
        self.events: list[str] = []
        self.hot_reload_enabled: bool | None = None

    async def set_hot_reload(self, enable: bool) -> DAPResponse:
        self.events.append("set_hot_reload")
        self.hot_reload_enabled = enable
        return DAPResponse(1, 1, True, "setHotReload")

    async def set_function_breakpoints(self, breakpoints: list[dict]) -> DAPResponse:
        self.events.append("set_function_breakpoints")
        return DAPResponse(1, 1, True, "setFunctionBreakpoints")

    async def set_exception_breakpoints(self, filters: list[str] | None = None) -> DAPResponse:
        self.events.append("set_exception_breakpoints")
        return DAPResponse(1, 1, True, "setExceptionBreakpoints")

    async def launch(self, **_kwargs) -> DAPResponse:
        self.events.append("launch")
        return DAPResponse(1, 1, True, "launch")

    async def configuration_done(self) -> DAPResponse:
        self.events.append("configuration_done")
        return DAPResponse(1, 1, True, "configurationDone")
