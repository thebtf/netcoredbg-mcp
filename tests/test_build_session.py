"""Tests for build session - per-workspace state machine."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.build.session import BuildSession
from netcoredbg_mcp.build.state import BuildError, BuildState
from netcoredbg_mcp.windows_process_owner import AdmissionStage, DrainStatus, ProcessAdmissionError
from tests.owner_scope_red import (
    BlockingStream,
    OwnedCommandProcess,
    PollingTimeoutStream,
    TreeProcess,
)


@pytest.fixture(autouse=True)
def _use_asyncio_compatibility_path(monkeypatch, request) -> None:
    """Keep existing cross-platform command tests on their established path."""
    if request.cls is not None and request.cls.__name__ == "TestOwnerScopedBuildMatrix":
        return
    monkeypatch.setattr("netcoredbg_mcp.build.session._IS_WINDOWS", False)


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
            await session.rebuild(str(project))

        assert len(commands) == 2
        assert "clean" in commands[0]
        assert "build" in commands[1]


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


class TestOwnerScopedBuildMatrix:
    """GREEN coverage for one retained Windows owner per build command."""

    @pytest.mark.asyncio
    async def test_o1_build_command_uses_one_admitted_owner_and_zero_accounting(
        self, tmp_path
    ) -> None:
        """O1: BuildSession consumes one admitted capability, never a reopened PID."""

        process = TreeProcess(pid=42001, initially_exited=True)
        owners: list[OwnedCommandProcess] = []
        launches: list[dict[str, object]] = []

        async def launch(**kwargs):
            launches.append(kwargs)
            owner = OwnedCommandProcess(process, kwargs["generation"])
            owners.append(owner)
            return owner

        session = BuildSession(workspace_root=str(tmp_path))
        with patch(
            "netcoredbg_mcp.build.session.WindowsOwnedProcess.launch",
            side_effect=launch,
        ):
            result = await session._run_command(["dotnet", "build"], timeout=0.1)

        assert result == (0, "", "")
        assert len(launches) == 1
        assert launches[0]["argv"] == ("dotnet", "build")
        assert launches[0]["stdin_mode"] == "devnull"
        assert owners[0].drain_calls == ["grace"]
        assert owners[0].receipt is not None
        assert owners[0].receipt.active_processes == 0
        assert owners[0].aclose_calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stage",
        [AdmissionStage.ASSIGN, AdmissionStage.VERIFY, AdmissionStage.RESUME],
        ids=["o2-assignment", "o3-verification", "o4-resume"],
    )
    async def test_o2_o3_o4_admission_failure_never_installs_a_command_owner(
        self,
        tmp_path,
        stage: AdmissionStage,
    ) -> None:
        """O2-O4: fail-closed admission errors escape without command execution."""

        session = BuildSession(workspace_root=str(tmp_path))
        error = ProcessAdmissionError(stage, "rejected-owner", 5)
        with (
            patch(
                "netcoredbg_mcp.build.session.WindowsOwnedProcess.launch",
                side_effect=error,
            ),
            pytest.raises(ProcessAdmissionError) as raised,
        ):
            await session._run_command(["dotnet", "build"], timeout=0.1)

        assert raised.value.stage is stage
        assert session._current_owner is None
        assert session._current_process is None
        assert session._last_owner_drain_receipt is None

    @pytest.mark.asyncio
    async def test_o7_timeout_drains_the_command_descendant_before_reporting_timeout(
        self, tmp_path
    ) -> None:
        """O7 timeout: preserve TimeoutError only after exact owner drain."""

        stdout = BlockingStream()
        stderr = BlockingStream()
        process = TreeProcess(pid=42007, stdout=stdout, stderr=stderr)
        owner = OwnedCommandProcess(process, "timeout")
        session = BuildSession(workspace_root=str(tmp_path))
        try:
            with (
                patch(
                    "netcoredbg_mcp.build.session.WindowsOwnedProcess.launch",
                    return_value=owner,
                ),
                pytest.raises(asyncio.TimeoutError),
            ):
                await session._run_command(["dotnet", "build"], timeout=0.01)
        finally:
            stdout.release.set()
            stderr.release.set()

        assert process.child_alive is False
        assert owner.drain_calls == ["force"]
        assert owner.drain_operation_count == 1
        assert owner.aclose_calls == 1

    @pytest.mark.asyncio
    async def test_o7_session_cancel_drains_the_command_descendant_before_completion(
        self, tmp_path
    ) -> None:
        """O7 session cancel: concurrent cleanup joins one retained owner drain."""

        stdout = PollingTimeoutStream()
        process = TreeProcess(pid=42008, stdout=stdout, stderr=PollingTimeoutStream())
        owner = OwnedCommandProcess(process, "session-cancel")
        session = BuildSession(workspace_root=str(tmp_path))
        session._state = BuildState.BUILDING
        with patch(
            "netcoredbg_mcp.build.session.WindowsOwnedProcess.launch",
            return_value=owner,
        ):
            task = asyncio.create_task(session._run_command(["dotnet", "build"], timeout=1.0))
            await asyncio.wait_for(stdout.read_started.wait(), timeout=1.0)
            await session.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1.0)

        assert process.child_alive is False
        assert owner.drain_operation_count == 1
        assert owner.aclose_calls == 1

    @pytest.mark.asyncio
    async def test_o7_outer_cancellation_does_not_orphan_a_real_command_descendant(
        self, tmp_path
    ) -> None:
        """O7 outer cancellation drains a real inherited command descendant."""

        import json
        import sys
        from pathlib import Path

        import psutil

        fixture = Path(__file__).parent / "fixtures" / "owner_scope_process_tree.py"
        root_marker = tmp_path / "root.json"
        child_marker = tmp_path / "child.json"
        release = tmp_path / "release"
        session = BuildSession(workspace_root=str(tmp_path))
        task = asyncio.create_task(
            session._run_command(
                [
                    sys.executable,
                    str(fixture),
                    "root",
                    str(root_marker),
                    str(child_marker),
                    str(release),
                ],
                timeout=30.0,
            )
        )
        child_pid: int | None = None
        try:
            for _ in range(300):
                if child_marker.exists():
                    child_pid = int(json.loads(child_marker.read_text(encoding="utf-8"))["pid"])
                    break
                await asyncio.sleep(0.01)
            assert child_pid is not None, "controlled descendant did not start"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(300):
                if not psutil.pid_exists(child_pid):
                    break
                await asyncio.sleep(0.01)
            assert psutil.pid_exists(child_pid) is False
            receipt = session._last_owner_drain_receipt
            assert receipt is not None
            assert receipt.status is DrainStatus.DRAINED
            assert receipt.active_processes == 0
        finally:
            release.touch()
            if child_pid is not None and psutil.pid_exists(child_pid):
                psutil.Process(child_pid).kill()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_o8_accounting_failure_cannot_claim_command_completion(self, tmp_path) -> None:
        """O8: a non-drained owner receipt fails normal command completion."""

        process = TreeProcess(pid=42009, initially_exited=True)
        owner = OwnedCommandProcess(
            process,
            "accounting-failure",
            normal_status=DrainStatus.FAILED,
        )
        session = BuildSession(workspace_root=str(tmp_path))
        with (
            patch(
                "netcoredbg_mcp.build.session.WindowsOwnedProcess.launch",
                return_value=owner,
            ),
            pytest.raises(RuntimeError, match="owner did not drain"),
        ):
            await session._run_command(["dotnet", "build"], timeout=0.1)

        assert owner.receipt is not None
        assert owner.receipt.status is DrainStatus.FAILED
        assert owner.aclose_calls == 1

    @pytest.mark.asyncio
    async def test_o11_lock_retry_never_falls_back_to_a_selector(self, tmp_path) -> None:
        """O11 remains RED until T008 deletes selector cleanup from retries."""

        session = BuildSession(workspace_root=str(tmp_path))
        session._run_command = AsyncMock(return_value=(1, "MSB3021 file in use", ""))
        selector = AsyncMock(return_value=0)

        with (
            patch("netcoredbg_mcp.build.session.cleanup_for_build", selector),
            patch("netcoredbg_mcp.build.session.asyncio.sleep", AsyncMock()),
        ):
            await session._run_build_with_retry(
                ["dotnet", "build"],
                project_path=str(tmp_path / "OwnerA.csproj"),
                configuration="Debug",
                timeout=0.1,
                retry_on_lock=True,
            )

        assert selector.await_count == 0, "current lock retry reaches selector cleanup"
