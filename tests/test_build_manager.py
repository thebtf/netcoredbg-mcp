"""Tests for build manager."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace
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
from netcoredbg_mcp.session import SessionManager
from netcoredbg_mcp.tools.debug import register_debug_tools
from netcoredbg_mcp.tools.output import register_output_tools
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


@pytest.fixture
def prebuild_diagnostics_session(tmp_path, monkeypatch, mock_netcoredbg_path):
    project = tmp_path / "App.csproj"
    project.write_text("answer = 42\n", encoding="utf-8")
    invalid_source = tmp_path / "Invalid.py"
    invalid_source.write_text("def broken(:\n", encoding="utf-8")
    manager = SessionManager(project_path=str(tmp_path))
    build_session = manager._build_manager.get_session(str(tmp_path))
    failed_commands: set[str] = set()
    commands: list[str] = []

    def command_line(command, project_path, configuration="Debug", extra_args=None):
        commands.append(command.value)
        source = str(invalid_source) if command.value in failed_commands else project_path
        return [
            sys.executable,
            "-c",
            "import json, os, py_compile, sys; "
            "print(json.dumps({'argv': sys.orig_argv, 'cwd': os.getcwd()}), flush=True); "
            "py_compile.compile(sys.argv[1], doraise=True)",
            source,
        ]

    monkeypatch.setattr(build_session._policy, "get_dotnet_command", command_line)
    return manager, project, failed_commands, commands


class TestFailedPreBuildDiagnostics:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("interrupted_stage", ["restore", "build"])
    @pytest.mark.parametrize("interruption", ["task_cancel", "manager_cancel_eof", "timeout"])
    @pytest.mark.parametrize(
        "command_backend",
        [
            "ordinary",
            pytest.param(
                "windows_owner",
                marks=pytest.mark.skipif(os.name != "nt", reason="Windows private-Job proof"),
            ),
        ],
    )
    async def test_interrupted_prebuild_omits_first_response_data(
        self,
        prebuild_diagnostics_session,
        capturing_mcp,
        monkeypatch,
        interrupted_stage,
        interruption,
        command_backend,
        burst_stdout=False,
    ):
        manager, project, _failed_commands, commands = prebuild_diagnostics_session
        build_session = manager._build_manager.get_session(str(project.parent))
        monkeypatch.setattr(
            "netcoredbg_mcp.build.session._IS_WINDOWS", command_backend == "windows_owner"
        )
        original_command_line = build_session._policy.get_dotnet_command

        def command_line(command, *args, **kwargs):
            if command.value != interrupted_stage:
                return original_command_line(command, *args, **kwargs)
            commands.append(command.value)
            return [
                sys.executable,
                "-c",
                "import sys, time; "
                "print('interrupted-stdout', flush=True); "
                "print('interrupted-stderr', file=sys.stderr, flush=True); "
                + (
                    "sys.stdout.buffer.write(b'x\\n' * 131072); sys.stdout.flush(); "
                    if burst_stdout
                    else ""
                )
                + "time.sleep(60)",
            ]

        monkeypatch.setattr(build_session._policy, "get_dotnet_command", command_line)
        if interruption == "timeout":
            build = build_session.build

            async def timed_build(project_path, command=BuildCommand.BUILD, *args, **kwargs):
                if command.value == interrupted_stage:
                    kwargs["timeout"] = 2.0
                return await build(project_path, command, *args, **kwargs)

            monkeypatch.setattr(build_session, "build", timed_build)

        ready = asyncio.Event()
        release = asyncio.Event()
        streams: set[str] = set()

        async def command_output(line):
            if line not in {"interrupted-stdout", "interrupted-stderr"}:
                return
            streams.add(line)
            if len(streams) == 2:
                ready.set()
            if interruption != "timeout":
                # Hold both readers between lines so explicit cancellation reaches EOF,
                # rather than relying on a readline timeout to classify the command.
                await release.wait()

        start_debugger = AsyncMock(side_effect=AssertionError("Debugger must not start"))
        monkeypatch.setattr(manager.client, "start", start_debugger)
        register_debug_tools(
            capturing_mcp,
            manager,
            ownership=SimpleNamespace(release=MagicMock()),
            notify_state_changed=AsyncMock(),
            check_session_access=lambda _ctx: None,
            execute_and_wait=AsyncMock(),
            resolve_project_root=AsyncMock(),
        )
        register_output_tools(capturing_mcp, manager)
        ctx = SimpleNamespace(
            report_progress=AsyncMock(),
            info=AsyncMock(side_effect=command_output),
            warning=AsyncMock(side_effect=command_output),
        )
        task = asyncio.create_task(
            capturing_mcp.tools["start_debug"](
                ctx,
                program=str(project.parent / "App.dll"),
                pre_build=True,
                build_project=str(project),
            )
        )
        process = None
        try:
            await asyncio.wait_for(ready.wait(), timeout=10.0)
            process = build_session._current_process
            assert process is not None
            if command_backend == "ordinary":
                assert isinstance(process, asyncio.subprocess.Process)
                if burst_stdout:

                    async def wait_for_paused_stdout():
                        while not process.stdout._paused:
                            await asyncio.sleep(0.01)

                    await asyncio.wait_for(wait_for_paused_stdout(), timeout=10.0)
                    print(
                        json.dumps(
                            {
                                "burst_stdout_bytes": 262144,
                                "paused_stdout_buffer_bytes": len(process.stdout._buffer),
                            }
                        )
                    )
            if interruption == "task_cancel":
                task.cancel()
            elif interruption == "manager_cancel_eof":
                assert await manager._build_manager.cancel(str(project.parent)) is True
                release.set()
            response = await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
            if command_backend == "ordinary":
                print(
                    json.dumps(
                        {
                            "interruption": interruption,
                            "stage": interrupted_stage,
                            "child_returncode_at_public_return": process.returncode,
                        }
                    )
                )
                assert process.returncode is not None, "Child survived public start_debug return"
        except BaseException:
            release.set()
            print(
                json.dumps(
                    {
                        "public_task_done_before_failure_hygiene": task.done(),
                        "child_returncode_before_failure_hygiene": (
                            process.returncode if process is not None else None
                        ),
                    }
                )
            )
            if command_backend == "ordinary" and process is not None:
                if process.returncode is None:
                    print(
                        "Failure-only hygiene: killing retained creator-owned child, not product PASS"
                    )
                    process.kill()
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
                print(f"Failure-only pipe drain: stdout={len(stdout)} stderr={len(stderr)}")
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            release.set()

        actual = manager.last_build_result
        assert actual is not None
        print(
            json.dumps(
                {"interruption": interruption, "result": actual.to_dict(), "response": response}
            )
        )
        assert actual is build_session.last_result
        assert actual.success is False
        assert actual.command == interrupted_stage
        assert commands == (["restore"] if interrupted_stage == "restore" else ["restore", "build"])
        start_debugger.assert_not_awaited()
        assert build_session._current_process is None
        assert build_session._current_owner is None
        if command_backend == "windows_owner":
            receipt = build_session._last_owner_drain_receipt
            assert receipt is not None
            assert receipt.status is DrainStatus.DRAINED
            assert receipt.active_processes == 0
        assert actual.state is (
            BuildState.FAILED if interruption == "timeout" else BuildState.CANCELLED
        )
        assert actual.cancelled is (interruption != "timeout")
        assert actual.exit_code is None
        assert response["error"]
        assert response["state"] == "idle"
        assert "data" not in response
        diagnostics = await capturing_mcp.tools["get_build_diagnostics"]()
        assert diagnostics["data"] == actual.to_dict()

    @pytest.mark.asyncio
    async def test_interrupted_prebuild_drains_paused_stdout(
        self, prebuild_diagnostics_session, capturing_mcp, monkeypatch
    ):
        await self.test_interrupted_prebuild_omits_first_response_data(
            prebuild_diagnostics_session,
            capturing_mcp,
            monkeypatch,
            interrupted_stage="build",
            interruption="task_cancel",
            command_backend="ordinary",
            burst_stdout=True,
        )

    @pytest.mark.asyncio
    async def test_unrelated_exception_omits_retained_failed_result(
        self, prebuild_diagnostics_session, capturing_mcp, monkeypatch
    ):
        manager, project, failed_commands, commands = prebuild_diagnostics_session
        failed_commands.add("build")
        with pytest.raises(BuildError):
            await manager.pre_launch_build(str(project), restore_first=False)
        previous = manager.last_build_result
        assert previous is not None
        assert previous.success is False
        register_debug_tools(
            capturing_mcp,
            manager,
            ownership=SimpleNamespace(release=MagicMock()),
            notify_state_changed=AsyncMock(),
            check_session_access=lambda _ctx: None,
            execute_and_wait=AsyncMock(),
            resolve_project_root=AsyncMock(
                side_effect=RuntimeError("Unrelated root resolution failure")
            ),
        )
        register_output_tools(capturing_mcp, manager)

        response = await capturing_mcp.tools["start_debug"](
            SimpleNamespace(), program=str(project.parent / "App.dll"), pre_build=False
        )

        assert response["error"] == "Unrelated root resolution failure"
        assert "data" not in response
        assert commands == ["build"]
        assert manager.last_build_result is previous
        diagnostics = await capturing_mcp.tools["get_build_diagnostics"]()
        assert diagnostics["data"] == previous.to_dict()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failed_stage", ["restore", "build"])
    async def test_failed_process_reaches_start_debug_response(
        self, prebuild_diagnostics_session, capturing_mcp, monkeypatch, failed_stage
    ):
        manager, project, failed_commands, commands = prebuild_diagnostics_session
        failed_commands.add(failed_stage)
        start_debugger = AsyncMock(side_effect=AssertionError("Debugger must not start"))
        monkeypatch.setattr(manager.client, "start", start_debugger)
        register_debug_tools(
            capturing_mcp,
            manager,
            ownership=SimpleNamespace(release=MagicMock()),
            notify_state_changed=AsyncMock(),
            check_session_access=lambda _ctx: None,
            execute_and_wait=AsyncMock(),
            resolve_project_root=AsyncMock(),
        )
        ctx = SimpleNamespace(report_progress=AsyncMock(), info=AsyncMock(), warning=AsyncMock())

        response = await capturing_mcp.tools["start_debug"](
            ctx,
            program=str(project.parent / "App.dll"),
            pre_build=True,
            build_project=str(project),
        )

        actual = manager.last_build_result
        assert actual is not None
        assert actual.success is False
        assert actual.command == failed_stage
        assert actual.exit_code == 1
        assert actual.diagnostics == []
        process_identity = json.loads(actual.stdout.splitlines()[0])
        assert actual.command_line == tuple(process_identity["argv"])
        assert os.path.samefile(actual.working_directory, process_identity["cwd"])
        assert "SyntaxError" in actual.stderr
        assert commands == (["restore"] if failed_stage == "restore" else ["restore", "build"])
        start_debugger.assert_not_awaited()
        assert response["error"]
        assert response["state"] == "idle"
        assert response["data"] == actual.to_dict()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failed_stage", ["restore", "build"])
    async def test_failed_process_reaches_public_diagnostics(
        self, prebuild_diagnostics_session, capturing_mcp, monkeypatch, failed_stage
    ):
        manager, project, failed_commands, commands = prebuild_diagnostics_session
        failed_commands.add(failed_stage)
        monkeypatch.setenv("NCD3_TEST_SECRET", "not-for-build-diagnostics")
        register_output_tools(capturing_mcp, manager)

        with pytest.raises(BuildError, match=f"{failed_stage.title()} failed: 0 errors") as error:
            await manager.pre_launch_build(str(project))

        actual = manager._build_manager.get_last_result(str(project.parent))
        assert actual is not None
        assert actual.success is False
        assert actual.command == failed_stage
        assert actual.exit_code == error.value.exit_code == 1
        assert actual.diagnostics == []
        process_identity = json.loads(actual.stdout.splitlines()[0])
        assert "SyntaxError" in actual.stderr
        response = await capturing_mcp.tools["get_build_diagnostics"]()
        data = response["data"]
        assert data["success"] is False
        assert data["state"] == "failed"
        assert data["command"] == failed_stage
        assert data["projectPath"] == str(project.resolve())
        assert data["configuration"] == "Debug"
        assert data["exitCode"] == actual.exit_code
        assert data["errorCount"] == 0
        assert data["stdout"] == actual.stdout
        assert data["stderr"] == actual.stderr
        assert data["commandLine"] == process_identity["argv"]
        assert os.path.samefile(data["workingDirectory"], process_identity["cwd"])
        assert actual.command_line == tuple(process_identity["argv"])
        assert actual.working_directory == str(project.parent)
        assert manager.last_build_result is actual
        assert commands == (["restore"] if failed_stage == "restore" else ["restore", "build"])
        assert "not-for-build-diagnostics" not in str(response)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failed_stage", ["clean", "rebuild"])
    async def test_failed_rebuild_preserves_executed_command(
        self, prebuild_diagnostics_session, failed_stage
    ):
        manager, project, failed_commands, commands = prebuild_diagnostics_session
        failed_commands.add(failed_stage)

        result = await manager._build_manager.build(
            str(project.parent), str(project), command=BuildCommand.REBUILD
        )

        process_identity = json.loads(result.stdout.splitlines()[0])
        data = result.to_dict()
        assert result.success is False
        assert result.exit_code == 1
        assert result.command == failed_stage
        assert result.command_line == tuple(process_identity["argv"])
        assert data["commandLine"] == process_identity["argv"]
        assert os.path.samefile(data["workingDirectory"], process_identity["cwd"])
        assert commands == (["clean"] if failed_stage == "clean" else ["clean", "rebuild"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rejected_attempt", ["missing_project", "spawn_failure", "cancelled"])
    async def test_rejected_attempt_does_not_publish_previous_result(
        self, prebuild_diagnostics_session, capturing_mcp, monkeypatch, rejected_attempt
    ):
        manager, project, _failed_commands, _commands = prebuild_diagnostics_session
        register_output_tools(capturing_mcp, manager)
        previous = await manager.pre_launch_build(str(project), restore_first=False)
        assert manager.last_build_result is previous

        if rejected_attempt == "missing_project":
            exception = ValueError
            project = project.parent / "Missing.csproj"
        elif rejected_attempt == "spawn_failure":
            exception = BuildError
            build_session = manager._build_manager.get_session(str(project.parent))
            monkeypatch.setattr(
                build_session._policy,
                "get_dotnet_command",
                lambda *_args, **_kwargs: [str(project.parent / "missing-build-command")],
            )
        else:
            exception = asyncio.CancelledError

            async def cancel_owner(_owner):
                raise asyncio.CancelledError

            monkeypatch.setattr("netcoredbg_mcp.build.manager.consume_prebuild_owner", cancel_owner)

        with pytest.raises(exception):
            await manager.pre_launch_build(str(project), restore_first=False)

        response = await capturing_mcp.tools["get_build_diagnostics"]()
        assert manager.last_build_result is None
        assert "command" not in response["data"]
        assert "stdout" not in response["data"]
        assert "stderr" not in response["data"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entry_point", ["pre_launch", "launch"])
    @pytest.mark.parametrize("refusal", ["capture", "drain"])
    async def test_owner_refusal_does_not_publish_previous_result(
        self, prebuild_diagnostics_session, capturing_mcp, monkeypatch, entry_point, refusal
    ):
        manager, project, _failed_commands, commands = prebuild_diagnostics_session
        register_output_tools(capturing_mcp, manager)
        previous = await manager.pre_launch_build(str(project), restore_first=False)
        assert previous.success is True
        assert manager.last_build_result is previous

        generation = "rejected-prebuild-generation"
        owner = OwnedProcessRef("rejected-prebuild-owner", generation, 44001)
        client = MagicMock(adapter_owner=None if refusal == "capture" else owner)
        manager._client = client
        manager._active_dap_run = generation
        if refusal == "capture":
            manager._windows_adapter_admission_generation = generation
            exception = RuntimeError
            message = "adapter admission is in progress"
        else:
            receipt = OwnerDrainReceipt(
                owner=owner,
                status=DrainStatus.FAILED,
                forced=False,
                root_returncode=None,
                active_processes=None,
            )
            monkeypatch.setattr(manager, "_stop_owned_adapter", AsyncMock(return_value=receipt))
            exception = PreBuildOwnerError
            message = None

        with pytest.raises(exception, match=message) as error:
            if entry_point == "pre_launch":
                await manager.pre_launch_build(str(project), restore_first=False)
            else:
                await manager.launch(
                    program=str(project.parent / "App.dll"),
                    pre_build=True,
                    build_project=str(project),
                )

        if refusal == "drain":
            assert error.value.outcome.receipt is receipt
        assert manager._client is client
        assert manager._active_dap_run == generation
        assert commands == ["build"]
        assert manager._build_manager.get_last_result(str(project.parent)) is previous
        response = await capturing_mcp.tools["get_build_diagnostics"]()
        assert manager.last_build_result is None
        assert response["data"] == {"message": "No build has been performed yet."}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("entry_point", "rejection", "requested_build", "access_error", "clear_result"),
        [
            ("start_debug", "missing_project", True, None, True),
            ("start_debug", "invalid_program", True, None, True),
            ("start_debug", "missing_project_path", True, None, True),
            ("start_debug", "root_resolution", True, None, True),
            ("restart_debug", "missing_config", True, None, True),
            ("restart_debug", "missing_project", True, None, True),
            ("restart_debug", "missing_project_path", True, None, True),
            ("restart_debug", "saved_prebuild_stop", False, None, True),
            ("start_debug", "missing_project", True, "Foreign session", False),
            ("restart_debug", "missing_config", True, "Foreign session", False),
            ("start_debug", "missing_program", False, None, False),
            ("restart_debug", "missing_config", False, None, False),
        ],
    )
    async def test_public_prebuild_validation_does_not_publish_previous_result(
        self,
        prebuild_diagnostics_session,
        capturing_mcp,
        monkeypatch,
        entry_point,
        rejection,
        requested_build,
        access_error,
        clear_result,
    ):
        manager, project, failed_commands, commands = prebuild_diagnostics_session
        if rejection in {"invalid_program", "missing_program"}:
            failed_commands.add("build")
            with pytest.raises(BuildError):
                await manager.pre_launch_build(str(project), restore_first=False)
            previous = manager.last_build_result
            assert previous is not None
            assert previous.success is False
        else:
            previous = await manager.pre_launch_build(str(project), restore_first=False)
            assert previous.success is True
        resolve_root = AsyncMock()
        if rejection == "root_resolution":
            resolve_root.side_effect = ValueError("Project root rejected")
        register_debug_tools(
            capturing_mcp,
            manager,
            ownership=SimpleNamespace(release=MagicMock()),
            notify_state_changed=AsyncMock(),
            check_session_access=lambda _ctx: access_error,
            execute_and_wait=AsyncMock(),
            resolve_project_root=resolve_root,
        )
        register_output_tools(capturing_mcp, manager)
        ctx = SimpleNamespace(report_progress=AsyncMock())
        if entry_point == "start_debug":
            program = project.parent / ("App.txt" if rejection == "invalid_program" else "App.dll")
            build_project = None if rejection == "missing_project" else str(project)
            if rejection == "missing_project_path":
                build_project = str(project.parent / "Missing.csproj")
            response = await capturing_mcp.tools[entry_point](
                ctx,
                program=str(program),
                pre_build=requested_build,
                build_project=build_project,
            )
        else:
            if rejection != "missing_config":
                manager._last_launch_config = {
                    "program": str(project.parent / "App.dll"),
                    "build_project": None if rejection == "missing_project" else str(project),
                    "build_configuration": "Debug",
                    "pre_build": rejection == "saved_prebuild_stop",
                }
                if rejection == "missing_project_path":
                    manager._last_launch_config["build_project"] = str(
                        project.parent / "Missing.csproj"
                    )
                if rejection == "saved_prebuild_stop":
                    monkeypatch.setattr(
                        manager, "_stop_locked", AsyncMock(side_effect=RuntimeError("Stop refused"))
                    )
            response = await capturing_mcp.tools[entry_point](ctx, rebuild=requested_build)

        assert response["error"]
        assert "data" not in response
        assert commands == ["build"]
        assert manager._build_manager.get_last_result(str(project.parent)) is previous
        diagnostics = await capturing_mcp.tools["get_build_diagnostics"]()
        if clear_result:
            assert manager.last_build_result is None
            assert diagnostics["data"] == {"message": "No build has been performed yet."}
        else:
            assert manager.last_build_result is previous
            assert diagnostics["data"] == previous.to_dict()
        if access_error:
            resolve_root.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entry_point", ["launch", "restart"])
    async def test_direct_prebuild_validation_does_not_publish_previous_result(
        self,
        prebuild_diagnostics_session,
        capturing_mcp,
        entry_point,
    ):
        manager, project, _failed_commands, commands = prebuild_diagnostics_session
        previous = await manager.pre_launch_build(str(project), restore_first=False)
        register_output_tools(capturing_mcp, manager)
        with pytest.raises((ValueError, RuntimeError)):
            if entry_point == "launch":
                await manager.launch(str(project.parent / "App.dll"), pre_build=True)
            else:
                await manager.restart(rebuild=True)
        assert commands == ["build"]
        assert manager._build_manager.get_last_result(str(project.parent)) is previous
        diagnostics = await capturing_mcp.tools["get_build_diagnostics"]()
        assert manager.last_build_result is None
        assert diagnostics["data"] == {"message": "No build has been performed yet."}
