"""Tests for build manager - singleton orchestrator."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.build.manager import BuildManager
from netcoredbg_mcp.build.policy import BuildCommand
from netcoredbg_mcp.build.session import BuildSession
from netcoredbg_mcp.build.state import BuildError, BuildState


class TestBuildManagerSingleton:
    """Tests for singleton pattern."""

    def test_singleton_returns_same_instance(self):
        """Test that BuildManager returns same instance."""
        # Reset singleton for test isolation
        BuildManager._instance = None

        manager1 = BuildManager()
        manager2 = BuildManager()

        assert manager1 is manager2

    def test_singleton_initialized_once(self):
        """Test that singleton is only initialized once."""
        BuildManager._instance = None

        manager1 = BuildManager()
        manager1._sessions["test"] = MagicMock()

        manager2 = BuildManager()

        # Sessions should persist
        assert "test" in manager2._sessions

        # Cleanup
        del manager2._sessions["test"]


class TestBuildManagerSessions:
    """Tests for session management."""

    def test_get_session_creates_new(self, tmp_path):
        """Test get_session creates new session if not exists."""
        BuildManager._instance = None
        manager = BuildManager()

        session = manager.get_session(str(tmp_path))

        assert session is not None
        assert isinstance(session, BuildSession)

    def test_get_session_returns_existing(self, tmp_path):
        """Test get_session returns existing session."""
        BuildManager._instance = None
        manager = BuildManager()

        session1 = manager.get_session(str(tmp_path))
        session2 = manager.get_session(str(tmp_path))

        assert session1 is session2

    def test_get_session_normalizes_path(self, tmp_path):
        """Test get_session normalizes paths."""
        BuildManager._instance = None
        manager = BuildManager()

        import os
        path_with_sep = str(tmp_path) + os.sep
        path_without = str(tmp_path)

        session1 = manager.get_session(path_with_sep)
        session2 = manager.get_session(path_without)

        assert session1 is session2

    def test_clear_session_removes(self, tmp_path):
        """Test clear_session removes session."""
        BuildManager._instance = None
        manager = BuildManager()

        manager.get_session(str(tmp_path))
        result = manager.clear_session(str(tmp_path))

        assert result is True
        assert manager.get_state(str(tmp_path)) is None

    def test_clear_session_nonexistent(self, tmp_path):
        """Test clear_session returns False if not exists."""
        BuildManager._instance = None
        manager = BuildManager()

        result = manager.clear_session(str(tmp_path / "nonexistent"))

        assert result is False


class TestBuildManagerStateListeners:
    """Tests for global state listeners."""

    def test_on_build_state_change_registers(self, tmp_path):
        """Test registering global listener."""
        BuildManager._instance = None
        manager = BuildManager()
        listener = MagicMock()

        manager.on_build_state_change(listener)

        assert len(manager._global_listeners) == 1

    def test_global_listener_called_on_state_change(self, tmp_path):
        """Test global listener called when session state changes."""
        BuildManager._instance = None
        manager = BuildManager()
        listener = MagicMock()
        manager.on_build_state_change(listener)

        session = manager.get_session(str(tmp_path))
        session._set_state(BuildState.BUILDING)

        listener.assert_called()


class TestBuildManagerBuild:
    """Tests for build execution through manager."""

    @pytest.mark.asyncio
    async def test_build_delegates_to_session(self, tmp_path):
        """Test build delegates to session."""
        BuildManager._instance = None
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.pid = None  # Avoid job object code path
            mock_process.returncode = 0
            mock_process.stdout = AsyncMock()
            mock_process.stdout.readline = AsyncMock(return_value=b"")
            mock_process.stderr = AsyncMock()
            mock_process.stderr.readline = AsyncMock(return_value=b"")
            mock_process.wait = AsyncMock(return_value=0)
            mock_exec.return_value = mock_process

            result = await manager.build(
                str(tmp_path), str(project), BuildCommand.BUILD
            )

        assert result.success is True

    @pytest.mark.asyncio
    async def test_build_with_relative_path(self, tmp_path):
        """Test build with relative project path."""
        BuildManager._instance = None
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.pid = None  # Avoid job object code path
            mock_process.returncode = 0
            mock_process.stdout = AsyncMock()
            mock_process.stdout.readline = AsyncMock(return_value=b"")
            mock_process.stderr = AsyncMock()
            mock_process.stderr.readline = AsyncMock(return_value=b"")
            mock_process.wait = AsyncMock(return_value=0)
            mock_exec.return_value = mock_process

            result = await manager.build(str(tmp_path), "Test.csproj")

        assert result.success is True


class TestBuildManagerPreLaunchBuild:
    """Tests for pre-launch build sequence."""

    @pytest.mark.asyncio
    async def test_pre_launch_build_restore_and_build(self, tmp_path):
        """Test pre-launch build runs restore then build."""
        BuildManager._instance = None
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()

        commands = []

        async def capture_exec(*args, **kwargs):
            commands.append(args)
            mock_process = AsyncMock()
            mock_process.pid = None  # Avoid job object code path
            mock_process.returncode = 0
            mock_process.stdout = AsyncMock()
            mock_process.stdout.readline = AsyncMock(return_value=b"")
            mock_process.stderr = AsyncMock()
            mock_process.stderr.readline = AsyncMock(return_value=b"")
            mock_process.wait = AsyncMock(return_value=0)
            return mock_process

        with patch("asyncio.create_subprocess_exec", capture_exec):
            result = await manager.pre_launch_build(
                str(tmp_path), str(project), restore_first=True
            )

        assert result.success is True
        # Filter to dotnet commands only (exclude cleanup taskkill calls)
        dotnet_commands = [c for c in commands if c[0] == "dotnet"]
        assert len(dotnet_commands) == 2
        assert "restore" in dotnet_commands[0]
        assert "build" in dotnet_commands[1]

    @pytest.mark.asyncio
    async def test_pre_launch_build_without_restore(self, tmp_path):
        """Test pre-launch build without restore."""
        BuildManager._instance = None
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()

        commands = []

        async def capture_exec(*args, **kwargs):
            commands.append(args)
            mock_process = AsyncMock()
            mock_process.pid = None  # Avoid job object code path
            mock_process.returncode = 0
            mock_process.stdout = AsyncMock()
            mock_process.stdout.readline = AsyncMock(return_value=b"")
            mock_process.stderr = AsyncMock()
            mock_process.stderr.readline = AsyncMock(return_value=b"")
            mock_process.wait = AsyncMock(return_value=0)
            return mock_process

        with patch("asyncio.create_subprocess_exec", capture_exec):
            result = await manager.pre_launch_build(
                str(tmp_path), str(project), restore_first=False
            )

        assert result.success is True
        # Filter to dotnet commands only (exclude cleanup taskkill calls)
        dotnet_commands = [c for c in commands if c[0] == "dotnet"]
        assert len(dotnet_commands) == 1
        assert "build" in dotnet_commands[0]

    @pytest.mark.asyncio
    async def test_pre_launch_build_restore_failure_raises(self, tmp_path):
        """Test pre-launch build raises on restore failure."""
        BuildManager._instance = None
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()

        async def fail_restore(*args, **kwargs):
            mock_process = AsyncMock()
            mock_process.pid = None  # Avoid job object code path
            # Fail on restore (dotnet restore), succeed on others (cleanup)
            is_restore = args[0] == "dotnet" and "restore" in args
            mock_process.returncode = 1 if is_restore else 0
            mock_process.stdout = AsyncMock()
            mock_process.stdout.readline = AsyncMock(return_value=b"")
            mock_process.stderr = AsyncMock()
            mock_process.stderr.readline = AsyncMock(return_value=b"")
            mock_process.wait = AsyncMock(return_value=mock_process.returncode)
            return mock_process

        with patch("asyncio.create_subprocess_exec", fail_restore):
            with pytest.raises(BuildError, match="Restore failed"):
                await manager.pre_launch_build(str(tmp_path), str(project))

    @pytest.mark.asyncio
    async def test_pre_launch_build_failure_raises(self, tmp_path):
        """Test pre-launch build raises on build failure."""
        BuildManager._instance = None
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()

        async def fail_build(*args, **kwargs):
            mock_process = AsyncMock()
            mock_process.pid = None  # Avoid job object code path
            # Fail on build (dotnet build), succeed on restore and cleanup
            is_build = args[0] == "dotnet" and "build" in args
            mock_process.returncode = 1 if is_build else 0
            mock_process.stdout = AsyncMock()
            mock_process.stdout.readline = AsyncMock(return_value=b"")
            mock_process.stderr = AsyncMock()
            mock_process.stderr.readline = AsyncMock(return_value=b"")
            mock_process.wait = AsyncMock(return_value=mock_process.returncode)
            return mock_process

        with patch("asyncio.create_subprocess_exec", fail_build):
            with pytest.raises(BuildError, match="Build failed"):
                await manager.pre_launch_build(str(tmp_path), str(project))


class TestBuildManagerCancel:
    """Tests for build cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_delegates_to_session(self, tmp_path):
        """Test cancel delegates to session."""
        BuildManager._instance = None
        manager = BuildManager()

        session = manager.get_session(str(tmp_path))
        session._state = BuildState.BUILDING
        session._current_process = MagicMock()
        session._current_process.kill = MagicMock()

        result = await manager.cancel(str(tmp_path))

        assert result is True

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_workspace(self, tmp_path):
        """Test cancel returns False for nonexistent workspace."""
        BuildManager._instance = None
        manager = BuildManager()

        result = await manager.cancel(str(tmp_path / "nonexistent"))

        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_all(self, tmp_path):
        """Test cancel_all cancels all running builds."""
        BuildManager._instance = None
        manager = BuildManager()

        # Create two sessions with running builds
        ws1 = tmp_path / "ws1"
        ws2 = tmp_path / "ws2"
        ws1.mkdir()
        ws2.mkdir()

        session1 = manager.get_session(str(ws1))
        session2 = manager.get_session(str(ws2))

        session1._state = BuildState.BUILDING
        session1._current_process = MagicMock()
        session1._current_process.kill = MagicMock()

        session2._state = BuildState.BUILDING
        session2._current_process = MagicMock()
        session2._current_process.kill = MagicMock()

        cancelled = await manager.cancel_all()

        assert cancelled == 2


class TestBuildManagerStatus:
    """Tests for manager status methods."""

    def test_get_state(self, tmp_path):
        """Test get_state returns session state."""
        BuildManager._instance = None
        manager = BuildManager()

        session = manager.get_session(str(tmp_path))
        session._state = BuildState.READY

        state = manager.get_state(str(tmp_path))

        assert state == BuildState.READY

    def test_get_state_nonexistent(self, tmp_path):
        """Test get_state returns None for nonexistent workspace."""
        BuildManager._instance = None
        manager = BuildManager()

        state = manager.get_state(str(tmp_path / "nonexistent"))

        assert state is None

    def test_get_last_result(self, tmp_path):
        """Test get_last_result returns session's last result."""
        BuildManager._instance = None
        manager = BuildManager()

        session = manager.get_session(str(tmp_path))
        session._last_result = MagicMock()

        result = manager.get_last_result(str(tmp_path))

        assert result is session._last_result

    def test_get_all_states(self, tmp_path):
        """Test get_all_states returns all session states."""
        BuildManager._instance = None
        manager = BuildManager()

        ws1 = tmp_path / "ws1"
        ws2 = tmp_path / "ws2"
        ws1.mkdir()
        ws2.mkdir()

        manager.get_session(str(ws1))._state = BuildState.READY
        manager.get_session(str(ws2))._state = BuildState.FAILED

        states = manager.get_all_states()

        assert len(states) == 2

    def test_to_dict(self, tmp_path):
        """Test to_dict returns manager status."""
        BuildManager._instance = None
        manager = BuildManager()

        session = manager.get_session(str(tmp_path))
        session._state = BuildState.READY

        d = manager.to_dict()

        assert "sessions" in d
        assert len(d["sessions"]) == 1
