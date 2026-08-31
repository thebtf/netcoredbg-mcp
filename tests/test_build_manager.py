"""Tests for build manager."""

from unittest.mock import AsyncMock, MagicMock

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
