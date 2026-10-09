"""Public startup, cleanup and detach regressions for process ownership."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp import __main__ as entrypoint
from netcoredbg_mcp.process_registry import CleanupOutcome, ProcessRegistry
from netcoredbg_mcp.session import DebugState, SessionManager
from netcoredbg_mcp.tools.process import register_process_tools
from netcoredbg_mcp.windows_process_owner import DrainStatus, OwnedProcessRef, OwnerDrainReceipt


class CapturingMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, **_kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register


@pytest.mark.asyncio
async def test_public_force_cleanup_joins_owner_and_reports_failure():
    registry = ProcessRegistry()

    async def unfinished():
        return CleanupOutcome(complete=False, error="retained owner did not drain")

    registry.register_owner(generation="owned", owner=object(), cleanup=unfinished)
    session = SimpleNamespace(
        process_registry=registry, state=SimpleNamespace(state=DebugState.IDLE)
    )
    mcp = CapturingMCP()
    register_process_tools(mcp, session, lambda ctx: None)
    result = await mcp.tools["cleanup_processes"](None, force=True)
    assert "error" in result
    assert result["data"]["complete"] is False
    assert result["data"]["remaining_owners"] == 1
    assert result["data"]["terminated"] == 0
    assert result["data"]["tree_terminated"] is None


@pytest.mark.asyncio
async def test_force_access_guard_does_not_invoke_owner():
    registry = ProcessRegistry()
    cleanup = AsyncMock(return_value=CleanupOutcome(complete=True))
    registry.register_owner(generation="owned", owner=object(), cleanup=cleanup)
    session = SimpleNamespace(
        process_registry=registry, state=SimpleNamespace(state=DebugState.IDLE)
    )
    mcp = CapturingMCP()
    register_process_tools(mcp, session, lambda ctx: "owned by another agent")
    assert "error" in await mcp.tools["cleanup_processes"](None, force=True)
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_ignores_forged_pid_file_and_finally_joins_owners(tmp_path, monkeypatch):
    pidfile = tmp_path / ".netcoredbg-mcp.pid"
    forged = json.dumps({"server_pid": 0, "processes": [{"pid": 44009, "role": "netcoredbg"}]})
    pidfile.write_text(forged)
    registry = ProcessRegistry()
    finalized = []

    async def cleanup():
        finalized.append("owner")
        return CleanupOutcome(complete=True)

    registry.register_owner(generation="terminal", owner=object(), cleanup=cleanup)
    session = SimpleNamespace(
        process_registry=registry,
        is_active=True,
        close_resource_update_notifications=AsyncMock(
            side_effect=RuntimeError("notification failure")
        ),
        stop=AsyncMock(side_effect=RuntimeError("stop failure")),
    )
    server = MagicMock()
    server._mcp_server.run = AsyncMock()

    @asynccontextmanager
    async def stdio():
        yield object(), object()

    monkeypatch.setattr(
        entrypoint,
        "parse_args",
        lambda: SimpleNamespace(
            project=str(tmp_path), project_from_cwd=False, setup=False, command=None
        ),
    )
    monkeypatch.setattr(entrypoint, "configure_logging", lambda: None)
    monkeypatch.setattr(entrypoint, "configure_project_root", lambda **kwargs: None)
    monkeypatch.setattr(entrypoint, "get_project_root_sync", lambda: tmp_path)
    monkeypatch.setattr(entrypoint, "create_server", lambda path: server)
    monkeypatch.setattr(entrypoint, "get_session", lambda: session)
    monkeypatch.setattr("mcp.server.stdio.stdio_server", stdio)
    monkeypatch.setattr(
        "netcoredbg_mcp.resource_updates.apply_subscribe_capability", lambda caps: None
    )
    await entrypoint.main()
    assert pidfile.read_text() == forged
    assert registry.get_all() == []
    assert finalized == ["owner"]
    session.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_owner", [False, True])
async def test_attached_stop_detaches_in_both_owner_branches(with_owner):
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()
    generation = "attached"
    owner = OwnedProcessRef("attached-adapter", generation, 44010) if with_owner else None
    client = MagicMock()
    client.adapter_owner = owner
    client.adapter_pid = 44010
    client.is_running = True
    client.disconnect = AsyncMock()
    client.stop = AsyncMock(
        return_value=OwnerDrainReceipt(
            owner=owner,
            status=DrainStatus.DRAINED,
            forced=False,
            root_returncode=0,
            active_processes=0,
        )
        if owner
        else None
    )
    manager._client = client
    manager._active_dap_run = generation
    manager._debuggee_mode = (generation, "attach")
    await manager.stop()
    client.disconnect.assert_awaited_once_with(terminate=False)


@pytest.mark.asyncio
async def test_attach_mode_is_bound_before_request_and_survives_request_failure():
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()
    client = MagicMock()
    client.is_running = True
    client.set_exception_breakpoints = AsyncMock()
    manager._client = client
    manager._active_dap_run = "early-attach"
    manager._initialized_event.set()
    manager._sync_all_breakpoints = AsyncMock()

    async def attach(*args, **kwargs):
        assert manager._debuggee_mode == ("early-attach", "attach")
        raise RuntimeError("attach transport failure")

    client.attach = AsyncMock(side_effect=attach)
    with pytest.raises(RuntimeError, match="attach transport failure"):
        await manager.attach(44011)
    assert manager._debuggee_mode == ("early-attach", "attach")


@pytest.mark.asyncio
async def test_posix_stop_cancellation_joins_owner_before_release(monkeypatch):
    from netcoredbg_mcp.posix_process_owner import PosixCleanupResult

    class CapturedOwner:
        returncode: int | None = None

    monkeypatch.setattr("netcoredbg_mcp.session.manager.PosixOwnedProcess", CapturedOwner)
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()
    native = CapturedOwner()
    entered, release = asyncio.Event(), asyncio.Event()
    client = MagicMock()
    client.is_running = False
    client.adapter_owner = None
    client.adapter_cleanup_owner = native

    async def stop():
        entered.set()
        await release.wait()
        native.returncode = 0
        return PosixCleanupResult(0, -9, True, True)

    client.stop = AsyncMock(side_effect=stop)
    manager._client = client
    manager._active_dap_run = "cancel-posix"
    manager._register_adapter_cleanup(client, "cancel-posix")
    task = asyncio.create_task(manager.stop())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert manager.process_registry.owner_count == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.process_registry.owner_count == 0
    assert manager._active_dap_run is None


@pytest.mark.asyncio
async def test_stale_manager_cleanup_does_not_stop_same_pid_new_generation():
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()
    old_client = MagicMock(adapter_cleanup_owner=SimpleNamespace(returncode=None))
    manager._client = old_client
    manager._active_dap_run = "old"
    manager._register_adapter_cleanup(old_client, "old")
    new_client = MagicMock(adapter_pid=44009)
    new_client.stop = AsyncMock()
    manager._client = new_client
    manager._active_dap_run = "new"
    result = await manager.process_registry.cleanup_all()
    assert result.complete is False and result.remaining_owners == 1
    new_client.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_session_owner_is_still_cleaned_by_public_force():
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()
    native = SimpleNamespace(returncode=0)
    client = MagicMock(adapter_cleanup_owner=native, adapter_owner=None, is_running=False)
    client.stop = AsyncMock(return_value=None)
    manager._client = client
    manager._active_dap_run = "terminal"
    manager._state.state = DebugState.TERMINATED
    manager._register_adapter_cleanup(client, "terminal")
    result = await manager.process_registry.cleanup_all()
    assert result.complete is True and result.terminated == 0
    client.stop.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("root_was_forced, expected_count", [(True, 1), (False, 0)])
async def test_public_cleanup_counts_native_confirmed_root_not_callback(
    root_was_forced, expected_count
):
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()
    native = SimpleNamespace(returncode=None)
    owner = OwnedProcessRef("count-owner", "count", 44030)
    receipt = OwnerDrainReceipt(
        owner=owner,
        status=DrainStatus.DRAINED,
        forced=root_was_forced,
        root_returncode=0,
        active_processes=0,
        root_was_forced=root_was_forced,
    )
    client = MagicMock(adapter_owner=owner, adapter_cleanup_owner=native, is_running=False)

    async def stop(**kwargs):
        native.returncode = 0
        client.adapter_cleanup_result = receipt
        return receipt

    client.stop = AsyncMock(side_effect=stop)
    manager._client = client
    manager._active_dap_run = "count"
    manager._register_adapter_cleanup(client, "count")
    result = await manager.process_registry.cleanup_all()
    assert result.complete is True and result.terminated == expected_count


@pytest.mark.asyncio
async def test_rejected_second_attach_does_not_reset_existing_target_state():
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()
    client = MagicMock(is_running=True)
    client.set_exception_breakpoints = AsyncMock()
    client.attach = AsyncMock()
    manager._client = client
    manager._active_dap_run = "existing"
    manager._debuggee_mode = ("existing", "attach")
    manager._state.process_id = 44041
    manager._initialized_event.set()
    manager._sync_all_breakpoints = AsyncMock()
    with pytest.raises(RuntimeError, match="Stop the previous"):
        await manager.attach(44042)
    assert manager.state.process_id == 44041
    client.set_exception_breakpoints.assert_not_awaited()
    client.attach.assert_not_awaited()


@pytest.mark.asyncio
async def test_posix_cleanup_does_not_count_stale_signal_exit_as_forced_root(monkeypatch):
    from netcoredbg_mcp.dap.client import DAPClient
    from netcoredbg_mcp.posix_process_owner import PosixCleanupResult, PosixOwnedProcess
    from tests.owner_scope_red import BlockingStream

    generation = "stale-posix-root-status"

    class CachedRootOwner(PosixOwnedProcess):
        def __init__(self):
            self.generation = generation
            self._pid = 44050
            self._returncode = None
            self.stdin = None
            self.stdout = BlockingStream()
            self.stderr = BlockingStream()
            self.root_exit_delivered = asyncio.Event()
            self.cleanup_calls = 0
            self.close_calls = 0
            self.native_result = PosixCleanupResult(
                root_returncode=-15,
                guardian_returncode=-9,
                group_signal_sent=True,
                complete=True,
            )

        async def wait(self):
            await self.root_exit_delivered.wait()
            return self.native_result.root_returncode

        async def cleanup(self, grace_timeout, force_timeout):
            self.cleanup_calls += 1
            # The root already exited by signal; only delayed control delivery
            # updates its cached status. The guardian cleans remaining descendants.
            self._returncode = self.native_result.root_returncode
            self.root_exit_delivered.set()
            self.stdout.release.set()
            self.stderr.release.set()
            return self.native_result

        async def aclose(self):
            self.close_calls += 1
            return self.native_result

    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()
    owner = CachedRootOwner()
    client = DAPClient("/private/adapter")
    monkeypatch.setattr("netcoredbg_mcp.dap.client.os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        "netcoredbg_mcp.dap.client.PosixOwnedProcess.launch", AsyncMock(return_value=owner)
    )
    await client.start(generation=generation)
    # Transport terminal observation is already known, but the native root
    # status has not arrived. Avoid a second disconnect to an exited adapter.
    client._process = None
    manager._client = client
    manager._active_dap_run = generation
    manager._state.state = DebugState.TERMINATED
    manager._register_adapter_cleanup(client, generation)

    result = await manager.process_registry.cleanup_all()

    assert result.complete is True and result.remaining_owners == 0
    assert owner.cleanup_calls == 1 and owner.close_calls == 1
    assert client.adapter_cleanup_result == owner.native_result
    assert result.terminated == 0


@pytest.mark.asyncio
async def test_successful_attach_without_process_event_connects_public_ui(monkeypatch):
    from netcoredbg_mcp.dap.protocol import DAPResponse
    from netcoredbg_mcp.tools.ui import register_ui_tools

    target_pid = 44060
    manager = SessionManager(netcoredbg_path="/private/adapter")
    manager.client._process = SimpleNamespace(pid=44059, returncode=None)
    manager._active_dap_run = "attach-without-process-event"
    manager._state.state = DebugState.INITIALIZING
    manager._initialized_event.set()
    manager._register_event_handlers()
    protocol_requests = []

    async def acknowledge(command, arguments=None, timeout=30.0):
        protocol_requests.append((command, arguments))
        if command == "attach":
            assert arguments["processId"] == target_pid
        # An adapter can acknowledge attach/configuration without a process event.
        # Exercise the actual DAPClient wrapper and manager response handling.
        return DAPResponse(seq=1, request_seq=1, success=True, command=command)

    monkeypatch.setattr(manager.client, "send_request", acknowledge)

    class AttachedTargetBackend:
        process_id = None

        def __init__(self):
            self.connections = []

        async def connect(self, pid):
            assert pid == target_pid
            self.process_id = pid
            self.connections.append(pid)

        async def find_element(
            self, automation_id=None, name=None, control_type=None, root_id=None, xpath=None
        ):
            assert self.process_id == target_pid
            assert (automation_id, root_id) == ("lblHeader", "mainWindow")
            return {"found": True, "automationId": "lblHeader", "name": "Attached fixture header"}

    backend = AttachedTargetBackend()

    def create_backend(*, process_registry):
        assert process_registry is manager.process_registry
        return backend

    monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", create_backend)
    mcp = CapturingMCP()
    register_ui_tools(mcp, manager, lambda ctx: None)

    attached = await manager.attach(target_pid)
    ui_result = await mcp.tools["ui_find_element"](automation_id="lblHeader", root_id="mainWindow")

    assert attached == {"success": True, "processId": target_pid}
    assert [command for command, _ in protocol_requests] == [
        "setExceptionBreakpoints",
        "attach",
        "configurationDone",
    ]
    assert "error" not in ui_result, ui_result
    assert ui_result["data"]["found"] is True
    assert manager.state.process_id == target_pid
    assert backend.connections == [target_pid]
    assert manager.process_registry.owner_count == 0


@pytest.mark.asyncio
async def test_failed_attach_ack_does_not_publish_target_pid(monkeypatch):
    from netcoredbg_mcp.dap.protocol import DAPResponse

    manager = SessionManager(netcoredbg_path="/private/adapter")
    manager.client._process = SimpleNamespace(pid=44069, returncode=None)
    manager._active_dap_run = "failed-attach-ack"
    manager._state.state = DebugState.INITIALIZING
    manager._initialized_event.set()
    requests = []

    async def acknowledge(command, arguments=None, timeout=30.0):
        requests.append(command)
        if command == "attach":
            assert arguments["processId"] == 44070
            return DAPResponse(
                seq=1, request_seq=1, success=False, command=command, message="attach rejected"
            )
        return DAPResponse(seq=1, request_seq=1, success=True, command=command)

    monkeypatch.setattr(manager.client, "send_request", acknowledge)
    with pytest.raises(RuntimeError, match="Attach failed: attach rejected"):
        await manager.attach(44070)

    assert manager.state.process_id is None
    assert manager.state.state is not DebugState.RUNNING
    assert "configurationDone" not in requests
    assert manager.process_registry.owner_count == 0
