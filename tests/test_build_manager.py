"""Tests for build manager."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.build.cleanup import (
    NoOwnedAdapter,
    OwnedAdapterCleanup,
    PreBuildOwnerError,
)
from netcoredbg_mcp.build.manager import BuildManager
from netcoredbg_mcp.build.policy import BuildCommand
from netcoredbg_mcp.build.session import BuildSession
from netcoredbg_mcp.build.state import BuildError, BuildState
from netcoredbg_mcp.windows_process_owner import DrainStatus, OwnedProcessRef, OwnerDrainReceipt


class TestBuildManagerSessions:
    """Tests for session management."""

    def test_get_session_creates_new(self, tmp_path):
        """Test get_session creates new session if not exists."""
        manager = BuildManager()

        session = manager.get_session(str(tmp_path))

        assert session is not None
        assert isinstance(session, BuildSession)

    def test_get_session_returns_existing(self, tmp_path):
        """Test get_session returns existing session."""
        manager = BuildManager()

        session1 = manager.get_session(str(tmp_path))
        session2 = manager.get_session(str(tmp_path))

        assert session1 is session2

    def test_get_session_normalizes_path(self, tmp_path):
        """Test get_session normalizes paths."""
        manager = BuildManager()

        import os

        path_with_sep = str(tmp_path) + os.sep
        path_without = str(tmp_path)

        session1 = manager.get_session(path_with_sep)
        session2 = manager.get_session(path_without)

        assert session1 is session2

    def test_clear_session_removes(self, tmp_path):
        """Test clear_session removes session."""
        manager = BuildManager()

        manager.get_session(str(tmp_path))
        result = manager.clear_session(str(tmp_path))

        assert result is True
        assert manager.get_state(str(tmp_path)) is None

    def test_clear_session_nonexistent(self, tmp_path):
        """Test clear_session returns False if not exists."""
        manager = BuildManager()

        result = manager.clear_session(str(tmp_path / "nonexistent"))

        assert result is False

    def test_two_instances_dont_share_sessions(self, tmp_path):
        """Test that two BuildManager instances have independent sessions."""
        manager1 = BuildManager()
        manager2 = BuildManager()

        session1 = manager1.get_session(str(tmp_path))
        session2 = manager2.get_session(str(tmp_path))

        assert session1 is not session2

    @pytest.mark.parametrize("status", [DrainStatus.FAILED, DrainStatus.TIMED_OUT])
    @pytest.mark.asyncio
    async def test_unresolved_command_owner_survives_clear_and_blocks_next_build(
        self, tmp_path, monkeypatch, status: DrainStatus
    ):
        project = tmp_path / "Test.csproj"
        project.touch()
        manager = BuildManager()
        session = manager.get_session(str(tmp_path))
        monkeypatch.setattr("netcoredbg_mcp.build.session._IS_WINDOWS", True)

        owner_a = MagicMock()
        owner_a.owner = OwnedProcessRef("owner-a", 1, 42001)
        owner_a.stdout.readline = AsyncMock(return_value=b"")
        owner_a.stderr.readline = AsyncMock(return_value=b"")
        owner_a.wait = AsyncMock(return_value=0)
        failed = OwnerDrainReceipt(owner_a.owner, status, True, 0, 1)
        drained_a = OwnerDrainReceipt(owner_a.owner, DrainStatus.DRAINED, True, 0, 0)
        owner_a._join_drain = AsyncMock(side_effect=[failed, failed, drained_a])
        owner_a.aclose = AsyncMock(side_effect=[failed, drained_a])

        owner_b = MagicMock()
        owner_b.owner = OwnedProcessRef("owner-b", 2, 42002)
        owner_b.stdout.readline = AsyncMock(return_value=b"")
        owner_b.stderr.readline = AsyncMock(return_value=b"")
        owner_b.wait = AsyncMock(return_value=0)
        drained_b = OwnerDrainReceipt(owner_b.owner, DrainStatus.DRAINED, False, 0, 0)
        owner_b._join_drain = AsyncMock(return_value=drained_b)
        owner_b.aclose = AsyncMock(return_value=drained_b)

        with patch(
            "netcoredbg_mcp.build.session.WindowsOwnedProcess.launch",
            new_callable=AsyncMock,
            side_effect=[owner_a, owner_b],
        ) as launch:
            with pytest.raises(BuildError, match="owner did not drain"):
                await manager.build(str(tmp_path), str(project))
            assert manager.clear_session(str(tmp_path)) is False
            assert manager.get_session(str(tmp_path)) is session

            with pytest.raises(BuildError, match="owner did not drain"):
                await manager.build(str(tmp_path), str(project))
            assert launch.await_count == 1
            assert owner_a._join_drain.await_count == 2
            assert manager.clear_session(str(tmp_path)) is False

            result = await manager.build(str(tmp_path), str(project))

        assert result.success is True
        assert launch.await_count == 2
        assert owner_a._join_drain.await_count == 3
        assert owner_a.aclose.await_count == 2
        assert session._current_owner is None
        assert manager.clear_session(str(tmp_path)) is True

    @pytest.mark.asyncio
    async def test_ready_listener_cannot_clear_a_session_with_queued_builds(self, tmp_path):
        project = tmp_path / "Test.csproj"
        project.touch()
        manager = BuildManager()
        session = manager.get_session(str(tmp_path))
        started_a = asyncio.Event()
        queued_b = asyncio.Event()
        started_b = asyncio.Event()
        selected_c = asyncio.Event()
        release_a = asyncio.Event()
        release_b = asyncio.Event()
        launched: list[BuildSession] = []
        clears: list[bool] = []
        c_session: list[BuildSession] = []
        c_task: asyncio.Task | None = None
        task_b: asyncio.Task | None = None

        async def command(current, *_args, **_kwargs):
            launched.append(current)
            if len(launched) == 1:
                started_a.set()
                await release_a.wait()
            elif len(launched) == 2:
                started_b.set()
                await release_b.wait()
            return 0, "", ""

        original_get_session = manager.get_session

        def get_session(path):
            result = original_get_session(path)
            if task_b is not None and asyncio.current_task() is task_b:
                queued_b.set()
            return result

        async def launch_c():
            c_session.append(manager.get_session(str(tmp_path)))
            selected_c.set()
            return await manager.build(str(tmp_path), str(project))

        def on_state(_workspace, state):
            nonlocal c_task
            if state is BuildState.READY and not clears:
                clears.append(manager.clear_session(str(tmp_path)))
                c_task = asyncio.create_task(launch_c())

        manager.on_build_state_change(on_state)
        with (
            patch.object(BuildSession, "_run_command", command),
            patch.object(manager, "get_session", get_session),
        ):
            task_a = asyncio.create_task(manager.build(str(tmp_path), str(project)))
            try:
                await asyncio.wait_for(started_a.wait(), 1.0)
                task_b = asyncio.create_task(manager.build(str(tmp_path), str(project)))
                await asyncio.wait_for(queued_b.wait(), 1.0)
                release_a.set()
                await asyncio.wait_for(selected_c.wait(), 1.0)
                assert clears == [False]
                assert c_session == [session]
                await asyncio.wait_for(started_b.wait(), 1.0)
                release_b.set()
                assert (await asyncio.wait_for(task_a, 1.0)).success
                assert (await asyncio.wait_for(task_b, 1.0)).success
                assert c_task is not None
                assert (await asyncio.wait_for(c_task, 1.0)).success
                assert launched == [session, session, session]
            finally:
                release_a.set()
                release_b.set()
                for task in (task_a, task_b, c_task):
                    if task is not None and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    def test_clear_session_does_not_discard_build_in_progress(self, tmp_path):
        manager = BuildManager()
        session = manager.get_session(str(tmp_path))
        session._set_state(BuildState.BUILDING)

        assert manager.clear_session(str(tmp_path)) is False
        assert manager.get_session(str(tmp_path)) is session


class TestBuildManagerStateListeners:
    """Tests for global state listeners."""

    def test_on_build_state_change_registers(self, tmp_path):
        """Test registering global listener."""
        manager = BuildManager()
        listener = MagicMock()

        manager.on_build_state_change(listener)

        assert len(manager._global_listeners) == 1

    def test_global_listener_called_on_state_change(self, tmp_path):
        """Test global listener called when session state changes."""
        manager = BuildManager()
        listener = MagicMock()
        manager.on_build_state_change(listener)

        session = manager.get_session(str(tmp_path))
        session._set_state(BuildState.BUILDING)

        listener.assert_called()


class TestBuildManagerBuild:
    """Tests for build delegation through the manager."""

    @pytest.mark.asyncio
    async def test_build_delegates_to_session(self, tmp_path):
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()
        session = manager.get_session(str(tmp_path))
        expected = MagicMock(success=True)
        session.build = AsyncMock(return_value=expected)

        result = await manager.build(str(tmp_path), str(project), BuildCommand.BUILD)

        assert result is expected
        session.build.assert_awaited_once_with(
            str(project), BuildCommand.BUILD, "Debug", None, 300.0
        )

    @pytest.mark.asyncio
    async def test_build_with_relative_path(self, tmp_path):
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()
        session = manager.get_session(str(tmp_path))
        expected = MagicMock(success=True)
        session.build = AsyncMock(return_value=expected)

        result = await manager.build(str(tmp_path), "Test.csproj")

        assert result is expected
        assert session.build.await_args.args[0] == str(project)


class TestBuildManagerPreLaunchBuild:
    """Tests for the owner-gated pre-launch build sequence."""

    @pytest.mark.asyncio
    async def test_pre_launch_build_restore_and_build(self, tmp_path):
        """A no-owner variant preserves restore followed by build."""
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()
        session = manager.get_session(str(tmp_path))
        events: list[str] = []

        async def restore(*_args, **_kwargs):
            events.append("restore")
            return MagicMock(success=True)

        async def build(*_args, **_kwargs):
            events.append("build")
            return MagicMock(success=True)

        session.restore = AsyncMock(side_effect=restore)
        session.build = AsyncMock(side_effect=build)

        result = await manager.pre_launch_build(
            str(tmp_path), str(project), owner=NoOwnedAdapter(), restore_first=True
        )

        assert result.success is True
        assert events == ["restore", "build"]

    @pytest.mark.asyncio
    async def test_pre_launch_build_without_restore(self, tmp_path):
        """A no-owner variant may build without a restore."""
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()
        session = manager.get_session(str(tmp_path))
        session.restore = AsyncMock()
        session.build = AsyncMock(return_value=MagicMock(success=True))

        result = await manager.pre_launch_build(
            str(tmp_path), str(project), owner=NoOwnedAdapter(), restore_first=False
        )

        assert result.success is True
        session.restore.assert_not_awaited()
        session.build.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pre_launch_build_restore_failure_raises(self, tmp_path):
        """A restore failure remains a BuildError after the owner gate passes."""
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()
        session = manager.get_session(str(tmp_path))
        session.restore = AsyncMock(
            return_value=MagicMock(success=False, error_count=1, diagnostics=[], exit_code=1)
        )
        session.build = AsyncMock()

        workspace_path = str(tmp_path)
        project_path = str(project)
        owner = NoOwnedAdapter()
        with pytest.raises(BuildError, match="Restore failed"):
            await manager.pre_launch_build(workspace_path, project_path, owner=owner)

        session.build.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pre_launch_build_failure_raises(self, tmp_path):
        """A build failure remains a BuildError after the owner gate passes."""
        manager = BuildManager()
        project = tmp_path / "Test.csproj"
        project.touch()
        session = manager.get_session(str(tmp_path))
        session.restore = AsyncMock(return_value=MagicMock(success=True))
        session.build = AsyncMock(
            return_value=MagicMock(success=False, error_count=1, diagnostics=[], exit_code=1)
        )

        workspace_path = str(tmp_path)
        project_path = str(project)
        owner = NoOwnedAdapter()
        with pytest.raises(BuildError, match="Build failed"):
            await manager.pre_launch_build(workspace_path, project_path, owner=owner)

    def test_pre_launch_build_requires_owner(self):
        """The internal cutover leaves no optional owner route."""
        import inspect

        parameter = inspect.signature(BuildManager.pre_launch_build).parameters["owner"]
        assert parameter.default is inspect.Parameter.empty


class TestBuildManagerCancel:
    """Tests for build cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_delegates_to_session(self, tmp_path):
        """Test cancel delegates to session."""
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
        manager = BuildManager()

        result = await manager.cancel(str(tmp_path / "nonexistent"))

        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_all(self, tmp_path):
        """Test cancel_all cancels all running builds."""
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
        manager = BuildManager()

        session = manager.get_session(str(tmp_path))
        session._state = BuildState.READY

        state = manager.get_state(str(tmp_path))

        assert state == BuildState.READY

    def test_get_state_nonexistent(self, tmp_path):
        """Test get_state returns None for nonexistent workspace."""
        manager = BuildManager()

        state = manager.get_state(str(tmp_path / "nonexistent"))

        assert state is None

    def test_get_last_result(self, tmp_path):
        """Test get_last_result returns session's last result."""
        manager = BuildManager()

        session = manager.get_session(str(tmp_path))
        session._last_result = MagicMock()

        result = manager.get_last_result(str(tmp_path))

        assert result is session._last_result

    def test_get_all_states(self, tmp_path):
        """Test get_all_states returns all session states."""
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
        manager = BuildManager()

        session = manager.get_session(str(tmp_path))
        session._state = BuildState.READY

        d = manager.to_dict()

        assert "sessions" in d
        assert len(d["sessions"]) == 1


class TestOwnerScopedPreBuild:
    """Behavior coverage for the explicit adapter-owner gate."""

    @pytest.mark.asyncio
    async def test_o10_prebuild_drains_only_captured_owner(self, tmp_path) -> None:
        """O10: owner A drains before build while owner B stays untouched."""
        manager = BuildManager()
        project = tmp_path / "OwnerA.csproj"
        project.touch()
        session = manager.get_session(str(tmp_path))
        session.build = AsyncMock(return_value=MagicMock(success=True))
        owner_a = OwnedProcessRef("owner-a", "generation-a", 44001)
        owner_b = OwnedProcessRef("owner-b", "generation-b", 44002)
        liveness = {
            "owner-a-root": True,
            "owner-a-descendant": True,
            "owner-b-root": True,
            "owner-b-descendant": True,
            "foreign-sentinel": True,
        }
        drained: list[OwnedProcessRef] = []

        async def drain(expected: OwnedProcessRef) -> OwnerDrainReceipt:
            drained.append(expected)
            liveness["owner-a-root"] = False
            liveness["owner-a-descendant"] = False
            return OwnerDrainReceipt(
                owner=expected,
                status=DrainStatus.DRAINED,
                forced=False,
                root_returncode=0,
                active_processes=0,
            )

        result = await manager.pre_launch_build(
            str(tmp_path),
            str(project),
            owner=OwnedAdapterCleanup(owner_a, drain),
            restore_first=False,
        )

        assert result.success is True
        assert drained == [owner_a]
        assert owner_b not in drained
        assert liveness == {
            "owner-a-root": False,
            "owner-a-descendant": False,
            "owner-b-root": True,
            "owner-b-descendant": True,
            "foreign-sentinel": True,
        }
        session.build.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "active_processes"),
        [
            (DrainStatus.STALE, None),
            (DrainStatus.FAILED, None),
            (DrainStatus.TIMED_OUT, 1),
            (DrainStatus.DRAINED, 1),
        ],
    )
    async def test_non_drained_owner_starts_no_restore_or_build(
        self,
        tmp_path,
        status: DrainStatus,
        active_processes: int | None,
    ) -> None:
        """A stale or nonzero drain receipt fails before any build command."""
        manager = BuildManager()
        project = tmp_path / "OwnerA.csproj"
        project.touch()
        session = manager.get_session(str(tmp_path))
        session.restore = AsyncMock()
        session.build = AsyncMock()
        owner = OwnedProcessRef("owner-a", "generation-a", 44001)
        receipt = OwnerDrainReceipt(
            owner=owner,
            status=status,
            forced=False,
            root_returncode=None,
            active_processes=active_processes,
        )

        async def drain(_expected: OwnedProcessRef) -> OwnerDrainReceipt:
            return receipt

        workspace_path = str(tmp_path)
        project_path = str(project)
        cleanup_adapter = OwnedAdapterCleanup(owner, drain)
        with pytest.raises(PreBuildOwnerError) as error:
            await manager.pre_launch_build(
                workspace_path,
                project_path,
                owner=cleanup_adapter,
            )

        assert error.value.outcome.receipt is receipt
        session.restore.assert_not_awaited()
        session.build.assert_not_awaited()
