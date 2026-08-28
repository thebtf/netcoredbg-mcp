"""Tests for build session - per-workspace state machine."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.build.session import BuildSession
from netcoredbg_mcp.build.state import BuildError, BuildState


class TestBuildSessionInit:
    """Tests for BuildSession initialization."""

    def test_init_with_workspace(self, tmp_path):
        """Test initialization with workspace."""
        session = BuildSession(workspace_root=str(tmp_path))

        assert session.workspace_root == str(tmp_path)
        assert session.state == BuildState.IDLE
        assert session.last_result is None

    def test_init_creates_policy(self, tmp_path):
        """Test that policy is created if not provided."""
        session = BuildSession(workspace_root=str(tmp_path))
        assert session._policy is not None
        assert session._policy.workspace_root == str(tmp_path)


class TestBuildSessionProperties:
    """Tests for BuildSession properties."""

    def test_is_building_false_when_idle(self, tmp_path):
        """Test is_building is False when idle."""
        session = BuildSession(workspace_root=str(tmp_path))
        assert not session.is_building

    def test_state_starts_idle(self, tmp_path):
        """Test initial state is IDLE."""
        session = BuildSession(workspace_root=str(tmp_path))
        assert session.state == BuildState.IDLE


class TestBuildSessionStateListeners:
    """Tests for state change listeners."""

    def test_on_state_change_registers_listener(self, tmp_path):
        """Test registering state change listener."""
        session = BuildSession(workspace_root=str(tmp_path))
        listener = MagicMock()

        session.on_state_change(listener)

        assert len(session._state_listeners) == 1

    def test_state_change_notifies_listeners(self, tmp_path):
        """Test that state changes notify listeners."""
        session = BuildSession(workspace_root=str(tmp_path))
        listener = MagicMock()
        session.on_state_change(listener)

        session._set_state(BuildState.BUILDING)

        listener.assert_called_once_with(BuildState.BUILDING)

    def test_listener_exception_doesnt_crash(self, tmp_path):
        """Test that listener exceptions don't crash session."""
        session = BuildSession(workspace_root=str(tmp_path))
        listener = MagicMock(side_effect=Exception("Listener error"))
        session.on_state_change(listener)

        # Should not raise
        session._set_state(BuildState.BUILDING)


class TestBuildSessionBuild:
    """Tests for build execution."""

    @pytest.mark.asyncio
    async def test_build_changes_state_to_building(self, tmp_path):
        """Test that build changes state to BUILDING."""
        project = tmp_path / "Test.csproj"
        project.touch()
        session = BuildSession(workspace_root=str(tmp_path))

        states = []
        session.on_state_change(lambda s: states.append(s))

        # Mock the subprocess
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

            await session.build(str(project))

        assert BuildState.BUILDING in states

    @pytest.mark.asyncio
    async def test_build_success_returns_ready_state(self, tmp_path):
        """Test successful build returns READY state."""
        project = tmp_path / "Test.csproj"
        project.touch()
        session = BuildSession(workspace_root=str(tmp_path))

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

            result = await session.build(str(project))

        assert result.success is True
        assert result.state == BuildState.READY
        assert session.state == BuildState.READY

    @pytest.mark.asyncio
    async def test_build_failure_returns_failed_state(self, tmp_path):
        """Test failed build returns FAILED state."""
        project = tmp_path / "Test.csproj"
        project.touch()
        session = BuildSession(workspace_root=str(tmp_path))

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.pid = None  # Avoid job object code path
            mock_process.returncode = 1
            mock_process.stdout = AsyncMock()
            mock_process.stdout.readline = AsyncMock(return_value=b"")
            mock_process.stderr = AsyncMock()
            mock_process.stderr.readline = AsyncMock(return_value=b"")
            mock_process.wait = AsyncMock(return_value=1)
            mock_exec.return_value = mock_process

            result = await session.build(str(project))

        assert result.success is False
        assert result.state == BuildState.FAILED
        assert session.state == BuildState.FAILED

    @pytest.mark.asyncio
    async def test_build_stores_last_result(self, tmp_path):
        """Test that build stores last result."""
        project = tmp_path / "Test.csproj"
        project.touch()
        session = BuildSession(workspace_root=str(tmp_path))

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

            await session.build(str(project))

        assert session.last_result is not None
        assert session.last_result.success is True

    @pytest.mark.asyncio
    async def test_build_invalid_project_raises_error(self, tmp_path):
        """Test that invalid project path raises BuildError."""
        session = BuildSession(workspace_root=str(tmp_path))

        with pytest.raises(BuildError, match="outside workspace"):
            await session.build("/etc/passwd")

    @pytest.mark.asyncio
    async def test_build_uses_configuration(self, tmp_path):
        """Test that build uses specified configuration."""
        project = tmp_path / "Test.csproj"
        project.touch()
        session = BuildSession(workspace_root=str(tmp_path))

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
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
            await session.build(str(project), configuration="Release")

        assert captured_cmd is not None
        assert "Release" in captured_cmd


class TestBuildSessionClean:
    """Tests for clean operation."""

    @pytest.mark.asyncio
    async def test_clean_runs_clean_command(self, tmp_path):
        """Test that clean runs dotnet clean."""
        project = tmp_path / "Test.csproj"
        project.touch()
        session = BuildSession(workspace_root=str(tmp_path))

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
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
            await session.clean(str(project))

        assert captured_cmd is not None
        assert "clean" in captured_cmd


class TestBuildSessionRestore:
    """Tests for restore operation."""

    @pytest.mark.asyncio
    async def test_restore_runs_restore_command(self, tmp_path):
        """Test that restore runs dotnet restore."""
        project = tmp_path / "Test.csproj"
        project.touch()
        session = BuildSession(workspace_root=str(tmp_path))

        captured_cmd = None

        async def capture_exec(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
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
            await session.restore(str(project))

        assert captured_cmd is not None
        assert "restore" in captured_cmd


class TestBuildSessionRebuild:
    """Tests for rebuild operation."""

    @pytest.mark.asyncio
    async def test_rebuild_runs_clean_then_build(self, tmp_path):
        """Test that rebuild runs clean then build."""
        project = tmp_path / "Test.csproj"
        project.touch()
        session = BuildSession(workspace_root=str(tmp_path))

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
            await session.rebuild(str(project), extra_args=["-v", "minimal"])

        assert commands == [
            ("dotnet", "clean", str(project), "-c", "Debug"),
            ("dotnet", "build", str(project), "-c", "Debug", "-v", "minimal"),
        ]


class TestBuildSessionCancellationSources:
    """Distinguish explicit build cancellation from external task cancellation."""

    @pytest.mark.asyncio
    async def test_build_returns_cancelled_result_for_session_requested_cancellation(
        self,
        tmp_path,
        monkeypatch,
    ):
        project = tmp_path / "Test.csproj"
        project.touch()
        session = BuildSession(workspace_root=str(tmp_path))

        async def cancel_from_session(**_kwargs):
            session._cancel_requested = True
            raise asyncio.CancelledError()

        monkeypatch.setattr(session, "_run_build_with_retry", cancel_from_session)

        result = await session.build(str(project))

        assert result.state == BuildState.CANCELLED
        assert result.cancelled is True
        assert session.state == BuildState.CANCELLED
        assert session.last_result is result

    @pytest.mark.asyncio
    async def test_build_propagates_external_cancellation_after_recording_result(
        self,
        tmp_path,
        monkeypatch,
    ):
        project = tmp_path / "Test.csproj"
        project.touch()
        session = BuildSession(workspace_root=str(tmp_path))

        async def cancel_externally(**_kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(session, "_run_build_with_retry", cancel_externally)

        with pytest.raises(asyncio.CancelledError):
            await session.build(str(project))

        assert session.state == BuildState.CANCELLED
        assert session.last_result is not None
        assert session.last_result.state == BuildState.CANCELLED
        assert session.last_result.cancelled is True


class _BlockingBuildStream:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self._release = asyncio.Event()

    async def readline(self) -> bytes:
        self.entered.set()
        await self._release.wait()
        return b""


def _blocking_build_process(stream: _BlockingBuildStream) -> MagicMock:
    process = MagicMock()
    process.pid = 42
    process.returncode = None
    process.stdout = stream
    process.stderr = stream
    process.kill = MagicMock()
    process.wait = AsyncMock(return_value=1)
    return process


async def _cancel_no_job_command(
    session: BuildSession,
    process: MagicMock,
    stream: _BlockingBuildStream,
    monkeypatch,
) -> None:
    async def no_job() -> None:
        return None

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(session, "_create_job_object", no_job)
    monkeypatch.setattr("asyncio.create_subprocess_exec", create_process)
    task = asyncio.create_task(session._run_command(["dotnet", "build"]))
    await stream.entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


class TestBuildSessionExternalCancellationCleanup:
    """External build cancellation must not orphan a no-Job child."""

    @pytest.mark.asyncio
    async def test_run_command_kills_and_awaits_no_job_child_on_external_cancellation(
        self,
        tmp_path,
        monkeypatch,
    ):
        session = BuildSession(workspace_root=str(tmp_path))
        stream = _BlockingBuildStream()
        process = _blocking_build_process(stream)

        await _cancel_no_job_command(session, process, stream, monkeypatch)

        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        assert session._current_process is None

    @pytest.mark.asyncio
    async def test_run_command_bounds_no_job_child_wait_on_external_cancellation(
        self,
        tmp_path,
        monkeypatch,
        caplog,
    ):
        session = BuildSession(workspace_root=str(tmp_path))
        stream = _BlockingBuildStream()
        process = _blocking_build_process(stream)
        caplog.set_level(logging.WARNING, logger="netcoredbg_mcp.build.session")

        async def never_finishes():
            await asyncio.Event().wait()

        process.wait = AsyncMock(side_effect=never_finishes)
        monkeypatch.setattr(
            "netcoredbg_mcp.build.session.EXTERNAL_CANCELLATION_PROCESS_WAIT_SECONDS",
            0.01,
            raising=False,
        )

        await _cancel_no_job_command(session, process, stream, monkeypatch)

        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        assert "Cancelled build process did not exit cleanly" in caplog.text

    @pytest.mark.asyncio
    async def test_run_command_does_not_kill_an_already_gone_child_on_external_cancellation(
        self,
        tmp_path,
        monkeypatch,
    ):
        session = BuildSession(workspace_root=str(tmp_path))
        stream = _BlockingBuildStream()
        process = _blocking_build_process(stream)
        process.returncode = 1

        await _cancel_no_job_command(session, process, stream, monkeypatch)

        process.kill.assert_not_called()
        process.wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_run_command_propagates_cancellation_when_no_job_kill_fails(
        self,
        tmp_path,
        monkeypatch,
        caplog,
    ):
        session = BuildSession(workspace_root=str(tmp_path))
        stream = _BlockingBuildStream()
        process = _blocking_build_process(stream)
        process.kill.side_effect = RuntimeError("kill unavailable")
        caplog.set_level(logging.WARNING, logger="netcoredbg_mcp.build.session")

        await _cancel_no_job_command(session, process, stream, monkeypatch)

        process.kill.assert_called_once()
        process.wait.assert_not_awaited()
        assert "Failed to kill externally cancelled build process" in caplog.text

    @pytest.mark.asyncio
    async def test_run_command_skips_external_cleanup_for_session_requested_cancellation(
        self,
        tmp_path,
        monkeypatch,
    ):
        session = BuildSession(workspace_root=str(tmp_path))
        session._cancel_requested = True
        stream = _BlockingBuildStream()
        process = _blocking_build_process(stream)

        await _cancel_no_job_command(session, process, stream, monkeypatch)

        process.kill.assert_not_called()
        process.wait.assert_not_awaited()


class TestBuildSessionCancel:
    """Tests for build cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_when_not_building(self, tmp_path):
        """Test cancel returns False when not building."""
        session = BuildSession(workspace_root=str(tmp_path))
        result = await session.cancel()
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_sets_cancel_flag(self, tmp_path):
        """Test that cancel sets the cancel flag."""
        session = BuildSession(workspace_root=str(tmp_path))
        session._state = BuildState.BUILDING
        session._cancel_requested = False

        # Mock process
        mock_process = MagicMock()
        mock_process.kill = MagicMock()
        session._current_process = mock_process

        result = await session.cancel()

        assert result is True
        assert session._cancel_requested is True
        mock_process.kill.assert_called_once()


class TestBuildSessionConcurrency:
    """Tests for build concurrency control."""

    @pytest.mark.asyncio
    async def test_concurrent_builds_serialized(self, tmp_path):
        """Test that concurrent builds are serialized."""
        project = tmp_path / "Test.csproj"
        project.touch()
        session = BuildSession(workspace_root=str(tmp_path))

        build_order = []

        async def mock_build(*args, **kwargs):
            build_order.append("start")
            await asyncio.sleep(0.1)
            build_order.append("end")
            mock_process = AsyncMock()
            mock_process.pid = None  # Avoid job object code path
            mock_process.returncode = 0
            mock_process.stdout = AsyncMock()
            mock_process.stdout.readline = AsyncMock(return_value=b"")
            mock_process.stderr = AsyncMock()
            mock_process.stderr.readline = AsyncMock(return_value=b"")
            mock_process.wait = AsyncMock(return_value=0)
            return mock_process

        with patch("asyncio.create_subprocess_exec", mock_build):
            # Start two builds concurrently
            task1 = asyncio.create_task(session.build(str(project)))
            task2 = asyncio.create_task(session.build(str(project)))

            await asyncio.gather(task1, task2)

        # Builds should be serialized: start-end-start-end not start-start-end-end
        assert build_order == ["start", "end", "start", "end"]
