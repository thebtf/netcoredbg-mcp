"""Provider admission, result handling, and session-owned write fencing.

Only process observation and the external one-operation runner are fake. The
public tool, shared adapter, and SessionManager remain real. Semantic fake
results are not evidence of real UIA assignment or same-target verification.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.dap.client import DAPClient, DapTerminalTrigger, _RunPhase
from netcoredbg_mcp.session.manager import SessionManager
from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
from netcoredbg_mcp.session.state import DebugState, TransportTerminalSummary
from netcoredbg_mcp.tools.ui_evidence import register_ui_evidence_tools
from tests.test_ui_text_input_safety import NoSafeTextSetterBackend, assert_no_hidden_input

_OMITTED = object()
_SELECTOR = {"automation_id": "CueTextBox", "control_type": "TextBox"}
_REQUEST_TEXT = "PRIVATE replacement payload 537"
_EXISTING_TEXT = "PRIVATE existing field payload 537"
_PROVIDER_DETAIL = "PRIVATE provider diagnostic 537"
_FORBIDDEN_KEYS = {
    "text",
    "value",
    "expected",
    "actual",
    "selected_text",
    "keys",
    "input_keys",
    "selection_keys",
    "full_tree",
    "raw_tree",
}


def outcome(status: str, effect_state: str, verified: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "effect_state": effect_state,
        "verified": verified,
    }
    if status != "PASS":
        result["reason"] = "Provider replacement did not establish the requested value."
    return result


def mark_transport_unavailable(session: SessionManager, marker: str) -> None:
    """Publish only synthetic stop/terminal facts; do not stop any real process."""
    assert session._active_dap_run is not None
    if marker == "stopping-generation":
        session._stopping_dap_run = session._active_dap_run
        return
    assert marker == "transport-terminal"
    session.state.transport_terminal = TransportTerminalSummary(
        first_observed_signal="stdout_eof",
        adapter_pid=43,
        process_exited=False,
        adapter_return_code=None,
        protocol_terminated=False,
        debuggee_exit_code=None,
        stdout_eof=True,
        last_dap_event_seq=None,
        last_dap_event_name=None,
        last_dap_event_body_preview=None,
        stderr_tail="",
        stderr_truncated=False,
        stderr_drained=False,
        reader_error=None,
        cleanup_outcome="failed",
    )


class FakeProcess:
    def __init__(self, creation_time: int = 100) -> None:
        self.pid = 42
        self.creation_time = creation_time
        self.exited = False
        self.observation_error = False


class FakeBinding:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.pid = process.pid
        self.creation_time = process.creation_time
        self.identity = (self.pid, self.creation_time)
        self.closed = False

    def is_signaled(self) -> bool:
        if self.closed:
            raise OSError("A closed observation handle does not prove process exit")
        if self.process.observation_error:
            raise OSError("The process wait failed without establishing exit")
        return self.process.exited

    def close(self) -> None:
        self.closed = True


class ControlledOperation:
    """An actually awaiting fake transport, with separate pre/post-write gates."""

    def __init__(
        self,
        result: Any = _OMITTED,
        *,
        error: BaseException | None = None,
        pause_before_dispatch: bool = False,
        pause_after_dispatch: bool = False,
    ) -> None:
        self.result = outcome("PASS", "completed", True) if result is _OMITTED else result
        self.error = error
        self.entered = asyncio.Event()
        self.allow_dispatch = asyncio.Event()
        self.dispatched = asyncio.Event()
        self.release = asyncio.Event()
        self.done = asyncio.Event()
        self.task: asyncio.Task[Any] | None = None
        if not pause_before_dispatch:
            self.allow_dispatch.set()
        if not pause_after_dispatch:
            self.release.set()


class ExternalRunner:
    """Record possible dispatches, not proof of provider assignment or input delivery."""

    def __init__(self) -> None:
        self.operations: deque[ControlledOperation] = deque()
        self.calls: list[dict[str, Any]] = []
        self.effects: list[dict[str, Any]] = []
        self.active: list[ControlledOperation] = []

    async def __call__(self, *, binding, selector, text, on_dispatch) -> Any:
        operation = self.operations.popleft() if self.operations else ControlledOperation()
        operation.task = asyncio.current_task()
        self.active.append(operation)
        call = {"binding": binding, "selector": dict(selector), "text": text}
        self.calls.append(call)
        operation.entered.set()
        try:
            await operation.allow_dispatch.wait()
            on_dispatch()
            self.effects.append(call)
            operation.dispatched.set()
            try:
                await operation.release.wait()
            except asyncio.CancelledError:
                # A remote provider may outlive waiter/helper cancellation.
                await operation.release.wait()
            if operation.error is not None:
                raise operation.error
            return operation.result
        finally:
            operation.done.set()

    async def finish(self, operation: ControlledOperation, result: Any = _OMITTED) -> None:
        if result is not _OMITTED:
            operation.result = result
        operation.release.set()
        await asyncio.wait_for(operation.done.wait(), timeout=1)
        if operation.task is not None:
            await asyncio.gather(operation.task, return_exceptions=True)
        # Allow the session receiver of that exact terminal return to resume.
        await asyncio.sleep(0)

    async def close(self) -> None:
        for operation in self.active:
            operation.allow_dispatch.set()
            operation.release.set()
        tasks = [operation.task for operation in self.active if operation.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class ProviderHarness:
    def __init__(self, boundary: str, capturing_mcp: Any, monkeypatch: Any) -> None:
        self.boundary = boundary
        self.registry = capturing_mcp
        self.runner = ExternalRunner()
        self.process = FakeProcess()
        self.handles: list[FakeBinding] = []
        self.capture_calls: list[int] = []
        self.backend = NoSafeTextSetterBackend()
        self.backend_holder = {"instance": self.backend}
        self.backend_factory = MagicMock(return_value=self.backend)
        self.backend_provider = AsyncMock(return_value=self.backend)
        monkeypatch.setattr("netcoredbg_mcp.ui.backend.create_backend", self.backend_factory)
        monkeypatch.setattr("netcoredbg_mcp.session.manager.DAPClient", MagicMock())
        self.session = SessionManager(netcoredbg_path="/unused-in-memory-dap")
        self.session._client = DAPClient("/unused-in-memory-dap")
        self.session._client._process = MagicMock(pid=43, returncode=None)
        self.session._active_dap_run = self.session._client._run_for_direct_reader().generation
        self.session.state.state = DebugState.RUNNING
        self.session.state.process_id = self.process.pid
        monkeypatch.setattr(
            self.session,
            "cancel_pending_stealth_foreground_restore",
            AsyncMock(return_value=False),
        )
        monkeypatch.setattr(
            self.session,
            "wait_for_pending_stealth_foreground_restore",
            AsyncMock(return_value=False),
        )
        provider_module = importlib.import_module("netcoredbg_mcp.ui.provider_text")
        monkeypatch.setattr(provider_module, "capture_process", self.capture_process)
        monkeypatch.setattr(provider_module, "run_provider_text", self.runner)
        self.envelopes: list[dict[str, Any]] = []

    def capture_process(self, pid: int) -> FakeBinding:
        self.capture_calls.append(pid)
        assert pid == self.process.pid
        binding = FakeBinding(self.process)
        self.handles.append(binding)
        return binding

    async def replace(
        self,
        *,
        boundary: str | None = None,
        text: Any = _REQUEST_TEXT,
        mode: Any = "provider_value",
        selector: dict[str, Any] | None = None,
        action: str = "set_text",
    ) -> dict[str, Any]:
        # Rebuild the entry on every invocation: a closure-local fence is insufficient.
        boundary = self.boundary if boundary is None else boundary
        selected = dict(_SELECTOR if selector is None else selector)
        args = {} if text is _OMITTED else {"text": text}
        if mode is not _OMITTED:
            args["replacement_mode"] = mode
        if boundary == "adapter":
            adapters = ui_operation_adapters(self.backend_provider, session=self.session)
            result = await adapters["ui.text.set_text"](selector=selected, **args)
            self.envelopes.append(result)
            return result
        register_ui_evidence_tools(
            mcp=self.registry,
            session=self.session,
            check_session_access=lambda _ctx: None,
            backend_holder=self.backend_holder,
        )
        response = await self.registry.tools["ui_text"](ctx=None, action=action, **selected, **args)
        self.envelopes.append(response)
        return response["data"]

    def assert_no_generic_input(self) -> None:
        assert_no_hidden_input(self.backend)
        self.backend_factory.assert_not_called()
        self.backend_provider.assert_not_awaited()
        self.session.cancel_pending_stealth_foreground_restore.assert_not_awaited()
        self.session.wait_for_pending_stealth_foreground_restore.assert_not_awaited()
        assert self.backend.read_calls == []

    def assert_not_admitted(self) -> None:
        self.assert_no_generic_input()
        assert self.capture_calls == []
        assert self.runner.calls == []
        assert self.runner.effects == []

    def reconnect_same_process(self) -> None:
        # Rebinding/epoch reset is deliberately NOT an observation of process exit.
        self.session._begin_debuggee_epoch(lifecycle_state=DebugState.RUNNING)
        self.session.state.process_id = self.process.pid
        self.backend_holder["instance"] = NoSafeTextSetterBackend()

    def bind_process_event(self, monkeypatch):
        client = DAPClient("/unused-in-memory-dap")
        client._process = MagicMock(pid=43, returncode=None)
        run = client._run_for_direct_reader()
        self.session._client = client
        self.session._active_dap_run = run.generation
        monkeypatch.setattr(self.session, "_observe_process", MagicMock())
        self.session._register_event_handlers()
        client._handle_message(
            {
                "seq": 1,
                "type": "event",
                "event": "process",
                "body": {"name": "in-memory debuggee", "systemProcessId": self.process.pid},
            },
            run,
        )
        assert self.session.state.process_name == "in-memory debuggee"
        return client, run


def assert_semantics(result, *, status: str, effect_state: str, verified: bool) -> None:
    assert result["status"] == status
    assert result["effect_state"] == effect_state
    assert result["verified"] is verified
    if status != "PASS":
        assert isinstance(result.get("reason"), str) and result["reason"].strip()


def assert_private(value: Any, *, text: str = _REQUEST_TEXT, logs: str = "") -> None:
    def inspect(item: Any) -> None:
        if isinstance(item, dict):
            assert not (_FORBIDDEN_KEYS & item.keys()), item
            for child in item.values():
                inspect(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                inspect(child)

    inspect(value)
    serialized = json.dumps(value, ensure_ascii=True)
    for private in (text, _EXISTING_TEXT, _PROVIDER_DETAIL):
        if private:
            escaped = json.dumps(private, ensure_ascii=True)[1:-1]
            assert escaped not in serialized
            assert private not in logs
            assert escaped not in logs


@pytest.fixture
async def harness_factory(capturing_mcp, monkeypatch):
    instances: list[ProviderHarness] = []

    def create(boundary: str) -> ProviderHarness:
        instance = ProviderHarness(boundary, capturing_mcp, monkeypatch)
        instances.append(instance)
        return instance

    try:
        yield create
    finally:
        for instance in instances:
            await instance.runner.close()


@pytest.fixture(params=["public", "adapter"])
def harness(request, harness_factory):
    return harness_factory(request.param)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [_OMITTED, None, "", "keyboard", "PROVIDER_VALUE"],
    ids=["omitted", "null", "empty", "keyboard", "wrong-case"],
)
async def test_explicit_admission_is_required_before_connection_or_process_capture(harness, mode):
    result = await harness.replace(mode=mode)
    harness.assert_not_admitted()
    assert_semantics(result, status="BLOCKED", effect_state="not_started", verified=False)
    assert_private(harness.envelopes)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["read", "get_state", "state", "assert_selection"])
async def test_replacement_mode_on_read_actions_refuses_before_effect(harness_factory, action):
    harness = harness_factory("public")
    result = await harness.replace(action=action)
    harness.assert_not_admitted()
    assert_semantics(result, status="BLOCKED", effect_state="not_started", verified=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["", _REQUEST_TEXT, " \t\r\n ", "^+%{ENTER}()~", "first\r\nsecond\tline", "A\U0001f680Z"],
    ids=["empty", "ordinary", "whitespace", "metacharacters", "multiline", "non-bmp"],
)
async def test_literal_admitted_text_reaches_one_provider_operation_without_input(harness, text):
    result = await harness.replace(text=text)
    assert len(harness.runner.effects) == 1
    assert harness.runner.effects[0]["text"] == text
    assert harness.runner.effects[0]["selector"] == _SELECTOR
    harness.assert_no_generic_input()
    assert_semantics(result, status="PASS", effect_state="completed", verified=True)
    assert_private(harness.envelopes, text=text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [_OMITTED, None, "bad\x00value", "bad\ud800value", "bad\udfffvalue"],
    ids=["omitted", "null", "nul", "unpaired-high", "unpaired-low"],
)
async def test_invalid_text_is_rejected_before_backend_or_effect(harness, text):
    result = await harness.replace(text=text)
    harness.assert_not_admitted()
    if harness.boundary == "public" and (text is _OMITTED or text is None):
        assert result["status"] == "FAIL"
        assert result.get("reason")
    else:
        assert_semantics(result, status="BLOCKED", effect_state="not_started", verified=False)
    assert_private(harness.envelopes, text=text if isinstance(text, str) else _REQUEST_TEXT)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector",
    [
        {},
        {"name": " \t"},
        {"automation_id": "CueTextBox", "root_id": "Editor"},
        {"automation_id": "CueTextBox", "root_id": ""},
        {"xpath": "//TextBox"},
        {"automation_id": "CueTextBox", "xpath": ""},
    ],
    ids=["missing", "blank", "root", "empty-root", "xpath", "empty-xpath"],
)
async def test_selector_constraints_fail_closed_before_process_capture(harness, selector):
    result = await harness.replace(selector=selector)
    harness.assert_not_admitted()
    if harness.boundary == "public" and not selector:
        assert result["status"] == "FAIL"
        assert result.get("reason")
    else:
        assert_semantics(result, status="BLOCKED", effect_state="not_started", verified=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector",
    [
        {"name": "Exact field name"},
        {"control_type": "TextBox"},
        {"automation_id": " CueTextBox ", "name": " Exact field ", "control_type": "TextBox"},
    ],
    ids=["name", "known-type", "exact-conjunctive"],
)
async def test_supported_selector_criteria_are_preserved_exactly(harness, selector):
    result = await harness.replace(selector=selector)
    assert len(harness.runner.effects) == 1
    assert harness.runner.effects[0]["selector"] == selector
    assert_semantics(result, status="PASS", effect_state="completed", verified=True)
    harness.assert_no_generic_input()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "canonical"),
    [
        ({"automationId": "CueTextBox", "controlType": "TextBox"}, _SELECTOR),
        ({"automationId": "CueTextBox", "control_type": "TextBox"}, _SELECTOR),
        ({"automation_id": "CueTextBox", "controlType": "TextBox"}, _SELECTOR),
        ({"automationId": "CueTextBox"}, {"automation_id": "CueTextBox"}),
        ({"controlType": "TextBox"}, {"control_type": "TextBox"}),
        (
            {"automation_id": "CueTextBox", "automationId": "CueTextBox",
             "control_type": "TextBox", "controlType": "TextBox", "name": "Exact field"},
            {**_SELECTOR, "name": "Exact field"},
        ),
    ],
    ids=["camel-case", "camel-id", "camel-type", "id-only", "type-only", "equal-duplicates"],
)
async def test_shared_adapter_selector_aliases_preserve_canonical_exact_criteria(
    harness_factory, selector, canonical
):
    harness = harness_factory("adapter")
    result = await harness.replace(selector=selector)

    assert_semantics(result, status="PASS", effect_state="completed", verified=True)
    assert len(harness.runner.effects) == 1
    assert harness.runner.effects[0]["selector"] == canonical
    harness.assert_no_generic_input()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector",
    [
        {"automation_id": "CueTextBox", "automationId": "OtherTextBox"},
        {"control_type": "TextBox", "controlType": "Button"},
        {"automation_id": "", "automationId": "CueTextBox"},
    ],
    ids=["conflicting-id", "conflicting-type", "empty-versus-nonempty"],
)
async def test_shared_adapter_conflicting_selector_aliases_refuse_without_guessing(
    harness_factory, selector
):
    harness = harness_factory("adapter")
    result = await harness.replace(selector=selector)

    assert_semantics(result, status="BLOCKED", effect_state="not_started", verified=False)
    assert harness.runner.effects == []
    harness.assert_no_generic_input()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [("rootAutomationId", "Editor"), ("rootAutomationId", ""),
     ("root_id", "Editor"), ("root_id", ""), ("xpath", "//TextBox"), ("xpath", "")],
    ids=["camel-root", "empty-camel-root", "snake-root", "empty-snake-root",
         "xpath", "empty-xpath"],
)
async def test_shared_adapter_alias_normalization_never_drops_disallowed_scope(
    harness_factory, key, value
):
    harness = harness_factory("adapter")
    result = await harness.replace(
        selector={"automationId": "CueTextBox", "controlType": "TextBox", key: value}
    )

    harness.assert_not_admitted()
    assert_semantics(result, status="BLOCKED", effect_state="not_started", verified=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "selector"),
    [
        ("selector not found", _SELECTOR),
        ("selector ambiguous", _SELECTOR),
        ("unsupported provider", _SELECTOR),
        ("unsupported control type", {"control_type": "NotARealControlType537"}),
    ],
    ids=["missing-target", "ambiguous-target", "unsupported-provider", "unknown-control-type"],
)
async def test_admitted_provider_preflight_refusal_does_not_poison_next_request(
    harness, reason, selector
):
    refused = outcome("BLOCKED", "not_started", False)
    refused["reason"] = reason
    harness.runner.operations.append(ControlledOperation(refused))
    first = await harness.replace(selector=selector)
    assert_semantics(first, status="BLOCKED", effect_state="not_started", verified=False)
    second = await harness.replace()
    assert_semantics(second, status="PASS", effect_state="completed", verified=True)
    assert len(harness.runner.calls) == 2
    harness.assert_no_generic_input()
    assert_private(harness.envelopes)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["PASS", "FAIL"], ids=["success", "mismatch"])
async def test_completed_operation_releases_fence_for_next_request(harness, status):
    harness.runner.operations.append(
        ControlledOperation(outcome(status, "completed", status == "PASS"))
    )
    first = await harness.replace()
    assert_semantics(first, status=status, effect_state="completed", verified=status == "PASS")
    second = await harness.replace(boundary="adapter" if harness.boundary == "public" else "public")
    assert_semantics(second, status="PASS", effect_state="completed", verified=True)
    assert len(harness.runner.effects) == 2
    harness.assert_no_generic_input()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "transport"])
async def test_uncertain_external_failure_fences_fresh_cross_entry_without_text_leaks(
    harness, failure, caplog
):
    caplog.set_level("DEBUG", logger="netcoredbg_mcp")
    error_type = TimeoutError if failure == "timeout" else RuntimeError
    harness.runner.operations.append(
        ControlledOperation(error=error_type(f"{_PROVIDER_DETAIL}: {_REQUEST_TEXT}"))
    )
    first = await harness.replace()
    assert_semantics(first, status="FAIL", effect_state="unknown", verified=False)
    other = "adapter" if harness.boundary == "public" else "public"
    second = await harness.replace(boundary=other)
    assert_semantics(second, status="BLOCKED", effect_state="not_started", verified=False)
    assert len(harness.runner.effects) == 1
    harness.assert_no_generic_input()
    assert_private(harness.envelopes, logs=caplog.text)


@pytest.mark.asyncio
async def test_unknown_effect_survives_reconnect_new_epoch_and_fresh_adapter(harness):
    harness.runner.operations.append(ControlledOperation(outcome("FAIL", "unknown", False)))
    first = await harness.replace()
    assert_semantics(first, status="FAIL", effect_state="unknown", verified=False)
    harness.reconnect_same_process()
    second = await harness.replace(boundary="adapter")
    assert_semantics(second, status="BLOCKED", effect_state="not_started", verified=False)
    assert len(harness.runner.effects) == 1
    assert not harness.process.exited


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reported",
    [
        None,
        {},
        {"status": "PASS"},
        outcome("PASS", "unknown", True),
        outcome("PASS", "completed", False),
        outcome("FAIL", "completed", True),
        outcome("BLOCKED", "unknown", False),
        {
            "jsonrpc": "2.0",
            "id": "unrelated-stale-operation",
            "result": outcome("PASS", "completed", True),
        },
    ],
    ids=[
        "null",
        "empty",
        "incomplete",
        "verified-unknown",
        "unverified-pass",
        "verified-fail",
        "blocked-unknown",
        "uncorrelated-wire-envelope",
    ],
)
async def test_malformed_or_contradictory_terminal_cannot_clear_fence(harness, reported):
    harness.runner.operations.append(ControlledOperation(reported))
    first = await harness.replace()
    assert_semantics(first, status="FAIL", effect_state="unknown", verified=False)
    second = await harness.replace(boundary="adapter")
    assert_semantics(second, status="BLOCKED", effect_state="not_started", verified=False)
    assert len(harness.runner.effects) == 1
    harness.assert_no_generic_input()


@pytest.mark.asyncio
async def test_concurrent_b_refuses_until_a_correlated_terminal_completion(harness):
    operation = ControlledOperation(pause_after_dispatch=True)
    harness.runner.operations.append(operation)
    caller = asyncio.create_task(harness.replace())
    try:
        await asyncio.wait_for(operation.dispatched.wait(), timeout=1)
        blocked = await asyncio.wait_for(harness.replace(boundary="adapter"), timeout=1)
        assert_semantics(blocked, status="BLOCKED", effect_state="not_started", verified=False)
        assert len(harness.runner.effects) == 1
        await harness.runner.finish(operation)
        completed = await asyncio.wait_for(caller, timeout=1)
        assert_semantics(completed, status="PASS", effect_state="completed", verified=True)
        next_request = await harness.replace(boundary="public")
        assert_semantics(next_request, status="PASS", effect_state="completed", verified=True)
        assert len(harness.runner.effects) == 2
        harness.assert_no_generic_input()
    finally:
        operation.release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "unknown", "uncorrelated"])
async def test_actual_cancellation_propagates_and_only_correlated_completion_releases_a(
    harness, terminal
):
    operation = ControlledOperation(pause_after_dispatch=True)
    harness.runner.operations.append(operation)
    caller = asyncio.create_task(harness.replace())
    try:
        await asyncio.wait_for(operation.dispatched.wait(), timeout=1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=1)
        blocked = await asyncio.wait_for(harness.replace(boundary="adapter"), timeout=1)
        assert_semantics(blocked, status="BLOCKED", effect_state="not_started", verified=False)
        assert len(harness.runner.effects) == 1
        if terminal == "completed":
            result = outcome("PASS", "completed", True)
        elif terminal == "unknown":
            result = outcome("FAIL", "unknown", False)
        else:
            result = {
                "jsonrpc": "2.0",
                "id": "stale-operation",
                "result": outcome("PASS", "completed", True),
            }
        await harness.runner.finish(operation, result)
        after_terminal = await asyncio.wait_for(harness.replace(boundary="public"), timeout=1)
        if terminal == "completed":
            assert_semantics(after_terminal, status="PASS", effect_state="completed", verified=True)
            assert len(harness.runner.effects) == 2
        else:
            assert_semantics(
                after_terminal, status="BLOCKED", effect_state="not_started", verified=False
            )
            assert len(harness.runner.effects) == 1
        harness.assert_no_generic_input()
    finally:
        operation.release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority", ["missing-generation", "missing-run", "stale-process", "stale-generation"]
)
async def test_unavailable_run_authority_refuses_provider_admission(harness, authority, caplog):
    caplog.set_level("DEBUG", logger="netcoredbg_mcp")
    if authority == "missing-generation":
        harness.session._active_dap_run = None
    elif authority == "missing-run":
        harness.session._client._run = None
    else:
        harness.session._client._process = MagicMock(pid=44, returncode=None)
        if authority == "stale-generation":
            harness.session._client._run_for_direct_reader()

    refused = await harness.replace()

    assert_semantics(refused, status="BLOCKED", effect_state="not_started", verified=False)
    assert harness.runner.effects == []
    harness.assert_no_generic_input()
    assert_private(harness.envelopes, logs=caplog.text)


@pytest.mark.asyncio
async def test_replaced_run_with_same_generation_refuses_reserved_dispatch(harness, caplog):
    caplog.set_level("DEBUG", logger="netcoredbg_mcp")
    client = harness.session._client
    original_run = client._run
    operation = ControlledOperation(pause_before_dispatch=True)
    harness.runner.operations.append(operation)
    caller = asyncio.create_task(harness.replace())
    try:
        await asyncio.wait_for(operation.entered.wait(), timeout=1)
        client._process = MagicMock(pid=44, returncode=None)
        replacement_run = client._run_for_direct_reader()
        replacement_run.generation = original_run.generation
        assert replacement_run is not original_run
        assert harness.session._active_dap_run == replacement_run.generation
        operation.allow_dispatch.set()
        refused = await asyncio.wait_for(caller, timeout=1)

        assert not operation.dispatched.is_set()
        assert harness.runner.effects == []
        assert_semantics(refused, status="BLOCKED", effect_state="not_started", verified=False)
        harness.assert_no_generic_input()
        assert_private(harness.envelopes, logs=caplog.text)
    finally:
        operation.allow_dispatch.set()
        operation.release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
async def test_finalizing_run_refuses_new_provider_admission(harness, monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="netcoredbg_mcp")
    client, run = harness.bind_process_event(monkeypatch)
    epoch = harness.session.state.activity_epoch
    finalizer_gate = asyncio.Event()

    async def hold_finalizer(_run):
        await finalizer_gate.wait()

    monkeypatch.setattr(client, "_finalize_run", hold_finalizer)
    finalizer, created = client._request_finalization(run, DapTerminalTrigger.STDOUT_EOF)
    try:
        assert created and run.phase is _RunPhase.FINALIZING
        assert harness.session.state.state is DebugState.RUNNING
        assert harness.session.state.transport_terminal is None
        assert harness.session.state.activity_epoch is epoch
        assert harness.session._active_dap_run == run.generation
        assert harness.session.state.process_id == harness.process.pid
        refused = await harness.replace()

        assert harness.runner.effects == []
        assert_semantics(refused, status="BLOCKED", effect_state="not_started", verified=False)
        harness.assert_no_generic_input()
        assert_private(harness.envelopes, logs=caplog.text)
    finally:
        finalizer.cancel()
        await asyncio.gather(finalizer, return_exceptions=True)


@pytest.mark.asyncio
async def test_finalizing_run_refuses_reserved_provider_dispatch(harness, monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="netcoredbg_mcp")
    client, run = harness.bind_process_event(monkeypatch)
    epoch = harness.session.state.activity_epoch
    operation = ControlledOperation(pause_before_dispatch=True)
    harness.runner.operations.append(operation)
    caller = asyncio.create_task(harness.replace())
    finalizer = None
    finalizer_gate = asyncio.Event()

    async def hold_finalizer(_run):
        await finalizer_gate.wait()

    monkeypatch.setattr(client, "_finalize_run", hold_finalizer)
    try:
        await asyncio.wait_for(operation.entered.wait(), timeout=1)
        assert not operation.dispatched.is_set()
        finalizer, created = client._request_finalization(run, DapTerminalTrigger.STDOUT_EOF)
        assert created and run.phase is _RunPhase.FINALIZING
        assert harness.session.state.state is DebugState.RUNNING
        assert harness.session.state.transport_terminal is None
        assert harness.session.state.activity_epoch is epoch
        assert harness.session._active_dap_run == run.generation
        assert harness.session.state.process_id == harness.process.pid
        assert not harness.process.exited
        operation.allow_dispatch.set()
        refused = await asyncio.wait_for(caller, timeout=1)

        assert not operation.dispatched.is_set()
        assert harness.runner.effects == []
        assert_semantics(refused, status="BLOCKED", effect_state="not_started", verified=False)
        harness.assert_no_generic_input()
        assert_private(harness.envelopes, logs=caplog.text)
    finally:
        operation.allow_dispatch.set()
        operation.release.set()
        caller.cancel()
        if finalizer is not None:
            finalizer.cancel()
        await asyncio.gather(
            caller, *([finalizer] if finalizer is not None else []), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("effect_state", ["completed", "unknown"])
async def test_finalizing_run_preserves_post_dispatch_effect_state(
    harness, monkeypatch, caplog, effect_state
):
    caplog.set_level("DEBUG", logger="netcoredbg_mcp")
    client, run = harness.bind_process_event(monkeypatch)
    operation = ControlledOperation(pause_after_dispatch=True)
    harness.runner.operations.append(operation)
    caller = asyncio.create_task(harness.replace())
    finalizer = None
    finalizer_gate = asyncio.Event()

    async def hold_finalizer(_run):
        await finalizer_gate.wait()

    monkeypatch.setattr(client, "_finalize_run", hold_finalizer)
    try:
        await asyncio.wait_for(operation.dispatched.wait(), timeout=1)
        finalizer, created = client._request_finalization(run, DapTerminalTrigger.STDOUT_EOF)
        assert created and run.phase is _RunPhase.FINALIZING
        assert harness.session.state.state is DebugState.RUNNING
        assert harness.session.state.transport_terminal is None
        reported = (
            outcome("PASS", "completed", True)
            if effect_state == "completed"
            else outcome("FAIL", "unknown", False)
        )
        await harness.runner.finish(operation, reported)
        result = await asyncio.wait_for(caller, timeout=1)

        assert_semantics(result, status="FAIL", effect_state=effect_state, verified=False)
        assert len(harness.runner.effects) == 1
        harness.assert_no_generic_input()
        assert_private(harness.envelopes, logs=caplog.text)
    finally:
        operation.release.set()
        caller.cancel()
        if finalizer is not None:
            finalizer.cancel()
        await asyncio.gather(
            caller, *([finalizer] if finalizer is not None else []), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["transport-terminal", "stopping-generation"])
async def test_transport_terminal_or_active_stop_refuses_new_provider_request(harness, marker):
    assert harness.session._active_dap_run is not None
    mark_transport_unavailable(harness.session, marker)
    assert harness.session.state.state == DebugState.RUNNING
    assert harness.session.state.process_id == harness.process.pid
    assert not harness.process.exited

    refused = await harness.replace()

    assert harness.runner.effects == []
    assert_semantics(refused, status="BLOCKED", effect_state="not_started", verified=False)
    harness.assert_no_generic_input()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["transport-terminal", "stopping-generation"])
async def test_transport_terminal_or_active_stop_before_dispatch_refuses_reserved_request(
    harness, marker
):
    generation = harness.session._active_dap_run
    original_epoch = harness.session.state.activity_epoch
    operation = ControlledOperation(pause_before_dispatch=True)
    harness.runner.operations.append(operation)
    caller = asyncio.create_task(harness.replace())
    try:
        await asyncio.wait_for(operation.entered.wait(), timeout=1)
        assert not operation.dispatched.is_set()
        mark_transport_unavailable(harness.session, marker)
        assert harness.session.state.state == DebugState.RUNNING
        assert harness.session.state.activity_epoch is original_epoch
        assert harness.session._active_dap_run is generation
        assert harness.session.state.process_id == harness.process.pid
        assert not harness.process.exited
        operation.allow_dispatch.set()
        refused = await asyncio.wait_for(caller, timeout=1)

        assert not operation.dispatched.is_set()
        assert harness.runner.effects == []
        assert_semantics(refused, status="BLOCKED", effect_state="not_started", verified=False)
        harness.assert_no_generic_input()
    finally:
        operation.allow_dispatch.set()
        operation.release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
async def test_transport_terminal_before_result_cannot_be_reported_as_verified_success(harness):
    generation = harness.session._active_dap_run
    original_epoch = harness.session.state.activity_epoch
    operation = ControlledOperation(pause_after_dispatch=True)
    harness.runner.operations.append(operation)
    caller = asyncio.create_task(harness.replace())
    try:
        await asyncio.wait_for(operation.dispatched.wait(), timeout=1)
        mark_transport_unavailable(harness.session, "transport-terminal")
        assert harness.session.state.state == DebugState.RUNNING
        assert harness.session.state.activity_epoch is original_epoch
        assert harness.session._active_dap_run is generation
        assert harness.session.state.process_id == harness.process.pid
        assert not harness.process.exited
        await harness.runner.finish(operation, outcome("PASS", "completed", True))
        result = await asyncio.wait_for(caller, timeout=1)

        assert result["status"] in {"FAIL", "BLOCKED"}
        assert result["effect_state"] in {"completed", "unknown"}
        assert result["verified"] is False
        assert result.get("reason")
        assert len(harness.runner.effects) == 1
        harness.assert_no_generic_input()
    finally:
        operation.release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
async def test_terminated_session_refuses_mutation_even_when_attached_process_is_alive(harness):
    harness.session.state.state = DebugState.TERMINATED
    assert harness.session.state.process_id == harness.process.pid
    assert not harness.process.exited

    refused = await harness.replace()

    assert harness.runner.effects == []
    assert_semantics(refused, status="BLOCKED", effect_state="not_started", verified=False)
    harness.assert_no_generic_input()


@pytest.mark.asyncio
async def test_terminal_transition_before_dispatch_refuses_old_provider_request(harness):
    original_epoch = harness.session.state.activity_epoch
    operation = ControlledOperation(pause_before_dispatch=True)
    harness.runner.operations.append(operation)
    caller = asyncio.create_task(harness.replace())
    try:
        await asyncio.wait_for(operation.entered.wait(), timeout=1)
        assert not operation.dispatched.is_set()
        harness.session.state.state = DebugState.TERMINATED
        assert harness.session.state.activity_epoch is original_epoch
        assert harness.session.state.process_id == harness.process.pid
        assert not harness.process.exited
        operation.allow_dispatch.set()
        refused = await asyncio.wait_for(caller, timeout=1)

        assert not operation.dispatched.is_set()
        assert harness.runner.effects == []
        assert_semantics(refused, status="BLOCKED", effect_state="not_started", verified=False)
        harness.assert_no_generic_input()
    finally:
        operation.allow_dispatch.set()
        operation.release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
async def test_old_completed_write_cannot_claim_current_epoch_success_or_release_newer_write(
    harness,
):
    operation_a = ControlledOperation(pause_after_dispatch=True)
    operation_b = ControlledOperation(pause_after_dispatch=True)
    harness.runner.operations.append(operation_a)
    caller_a = asyncio.create_task(harness.replace())
    caller_b = None
    try:
        await asyncio.wait_for(operation_a.dispatched.wait(), timeout=1)
        harness.reconnect_same_process()
        blocked = await asyncio.wait_for(harness.replace(boundary="adapter"), timeout=1)
        assert_semantics(blocked, status="BLOCKED", effect_state="not_started", verified=False)
        assert len(harness.runner.effects) == 1

        await harness.runner.finish(operation_a, outcome("PASS", "completed", True))
        old_result = await asyncio.wait_for(caller_a, timeout=1)
        assert old_result["status"] in {"FAIL", "BLOCKED"}
        assert old_result["effect_state"] in {"completed", "unknown"}
        assert old_result["verified"] is False
        assert old_result.get("reason")

        harness.runner.operations.append(operation_b)
        caller_b = asyncio.create_task(harness.replace(boundary="adapter"))
        await asyncio.wait_for(operation_b.dispatched.wait(), timeout=1)
        refused_c = await asyncio.wait_for(harness.replace(boundary="public"), timeout=1)
        assert_semantics(refused_c, status="BLOCKED", effect_state="not_started", verified=False)
        assert len(harness.runner.effects) == 2

        await harness.runner.finish(operation_b)
        new_result = await asyncio.wait_for(caller_b, timeout=1)
        assert_semantics(new_result, status="PASS", effect_state="completed", verified=True)
        harness.assert_no_generic_input()
    finally:
        operation_a.release.set()
        operation_b.release.set()
        caller_a.cancel()
        callers = [caller_a]
        if caller_b is not None:
            caller_b.cancel()
            callers.append(caller_b)
        await asyncio.gather(*callers, return_exceptions=True)


@pytest.mark.asyncio
async def test_old_request_cannot_dispatch_after_same_process_session_rebinding(harness):
    original_epoch = harness.session.state.activity_epoch
    original_pid = harness.session.state.process_id
    operation = ControlledOperation(pause_before_dispatch=True)
    harness.runner.operations.append(operation)
    caller = asyncio.create_task(harness.replace())
    try:
        await asyncio.wait_for(operation.entered.wait(), timeout=1)
        assert not operation.dispatched.is_set()

        harness.reconnect_same_process()
        assert harness.session.state.activity_epoch is not original_epoch
        assert harness.session.state.process_id == original_pid
        assert not harness.process.exited
        operation.allow_dispatch.set()
        refused = await asyncio.wait_for(caller, timeout=1)

        assert not operation.dispatched.is_set()
        assert harness.runner.effects == []
        assert_semantics(refused, status="BLOCKED", effect_state="not_started", verified=False)
        next_request = await harness.replace(boundary="adapter")
        assert_semantics(next_request, status="PASS", effect_state="completed", verified=True)
        assert len(harness.runner.effects) == 1
        harness.assert_no_generic_input()
    finally:
        operation.allow_dispatch.set()
        operation.release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancellation_before_possible_dispatch_does_not_fabricate_unknown_effect(harness):
    operation = ControlledOperation(pause_before_dispatch=True)
    harness.runner.operations.append(operation)
    caller = asyncio.create_task(harness.replace())
    try:
        await asyncio.wait_for(operation.entered.wait(), timeout=1)
        assert not operation.dispatched.is_set()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=1)
        operation.allow_dispatch.set()
        await asyncio.wait_for(operation.done.wait(), timeout=1)
        assert not operation.dispatched.is_set()
        assert harness.runner.effects == []
        next_request = await harness.replace(boundary="adapter")
        assert_semantics(next_request, status="PASS", effect_state="completed", verified=True)
        assert len(harness.runner.effects) == 1
        harness.assert_no_generic_input()
    finally:
        operation.allow_dispatch.set()
        operation.release.set()
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_original_process_wait_does_not_clear_unknown_fence(harness):
    harness.runner.operations.append(ControlledOperation(outcome("FAIL", "unknown", False)))
    first = await harness.replace()
    assert_semantics(first, status="FAIL", effect_state="unknown", verified=False)
    harness.process.observation_error = True
    harness.reconnect_same_process()
    second = await harness.replace(boundary="adapter")
    assert_semantics(second, status="BLOCKED", effect_state="not_started", verified=False)
    assert len(harness.runner.effects) == 1
    assert not harness.process.exited


@pytest.mark.asyncio
async def test_original_process_exit_allows_new_instance_even_when_pid_is_reused(harness):
    original = harness.process
    harness.runner.operations.append(ControlledOperation(outcome("FAIL", "unknown", False)))
    first = await harness.replace()
    assert_semantics(first, status="FAIL", effect_state="unknown", verified=False)
    original.exited = True
    harness.process = FakeProcess(creation_time=original.creation_time + 1)
    harness.reconnect_same_process()
    second = await harness.replace(boundary="adapter")
    assert_semantics(second, status="PASS", effect_state="completed", verified=True)
    assert len(harness.runner.effects) == 2
    assert (
        harness.runner.effects[0]["binding"].identity
        != harness.runner.effects[1]["binding"].identity
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "effect_state", "verified"),
    [("PASS", "completed", True), ("FAIL", "completed", False), ("FAIL", "unknown", False)],
    ids=["success", "mismatch", "unknown"],
)
async def test_full_envelope_and_captured_logs_do_not_echo_raw_provider_reason_or_text(
    harness, status, effect_state, verified, caplog
):
    caplog.set_level("DEBUG", logger="netcoredbg_mcp")
    reported = outcome(status, effect_state, verified)
    reported.update(
        reason=f"{_PROVIDER_DETAIL}; requested={_REQUEST_TEXT}; existing={_EXISTING_TEXT}",
        text=_REQUEST_TEXT,
        expected=_REQUEST_TEXT,
        actual=_EXISTING_TEXT,
        keys="^a",
        input_keys=_REQUEST_TEXT,
        result={"full_tree": {"value": _EXISTING_TEXT}},
        error=_PROVIDER_DETAIL,
    )
    harness.runner.operations.append(ControlledOperation(reported))
    result = await harness.replace()
    assert_semantics(result, status=status, effect_state=effect_state, verified=verified)
    assert len(harness.runner.effects) == 1
    harness.assert_no_generic_input()
    assert_private(harness.envelopes, logs=caplog.text)
