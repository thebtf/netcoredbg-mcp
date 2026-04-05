"""Tests for LOW priority DAP features: L2 stepInTargets, L3 allThreadsContinued,
L5 variable paging, L6 output variablesReference."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.dap import DAPEvent, DAPResponse
from netcoredbg_mcp.dap.protocol import Commands
from netcoredbg_mcp.session import DebugState, SessionManager
from netcoredbg_mcp.session.state import OutputEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_manager() -> SessionManager:
    """Create a SessionManager with a mocked DAPClient."""
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        mgr = SessionManager()
    return mgr


def make_dap_response(
    success: bool = True,
    body: dict | None = None,
    message: str | None = None,
) -> DAPResponse:
    return DAPResponse(
        seq=1,
        request_seq=1,
        success=success,
        command="test",
        message=message,
        body=body or {},
    )


# ---------------------------------------------------------------------------
# L2: stepInTargets — protocol.py
# ---------------------------------------------------------------------------

class TestStepInTargetsCommand:
    def test_command_value(self):
        assert Commands.STEP_IN_TARGETS == "stepInTargets"


# ---------------------------------------------------------------------------
# L2: stepInTargets — dap/client.py
# ---------------------------------------------------------------------------

class TestDAPClientStepInTargets:
    @pytest.mark.asyncio
    async def test_step_in_targets_sends_correct_request(self):
        """step_in_targets calls send_request with correct command and frameId."""
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            mgr = make_manager()

        from netcoredbg_mcp.dap.client import DAPClient
        client = DAPClient.__new__(DAPClient)
        client.send_request = AsyncMock(return_value=make_dap_response(
            body={"targets": [{"id": 1, "label": "Foo()"}]}
        ))
        client._capabilities = {}

        response = await client.step_in_targets(frame_id=42)
        client.send_request.assert_called_once_with(
            Commands.STEP_IN_TARGETS, {"frameId": 42}
        )
        assert response.success

    @pytest.mark.asyncio
    async def test_step_in_with_target_id(self):
        """step_in passes targetId when provided."""
        from netcoredbg_mcp.dap.client import DAPClient
        client = DAPClient.__new__(DAPClient)
        client.send_request = AsyncMock(return_value=make_dap_response())
        client._capabilities = {}

        await client.step_in(thread_id=1, target_id=3)
        client.send_request.assert_called_once_with(
            Commands.STEP_IN, {"threadId": 1, "targetId": 3}
        )

    @pytest.mark.asyncio
    async def test_step_in_without_target_id(self):
        """step_in omits targetId when not provided."""
        from netcoredbg_mcp.dap.client import DAPClient
        client = DAPClient.__new__(DAPClient)
        client.send_request = AsyncMock(return_value=make_dap_response())
        client._capabilities = {}

        await client.step_in(thread_id=1)
        client.send_request.assert_called_once_with(
            Commands.STEP_IN, {"threadId": 1}
        )


# ---------------------------------------------------------------------------
# L2: stepInTargets — session/manager.py
# ---------------------------------------------------------------------------

class TestSessionManagerStepInTargets:
    @pytest.mark.asyncio
    async def test_get_step_in_targets_returns_list(self):
        """get_step_in_targets returns list of {id, label} dicts."""
        mgr = make_manager()
        mgr._state.current_frame_id = 10
        mgr._client.step_in_targets = AsyncMock(return_value=make_dap_response(
            body={"targets": [
                {"id": 1, "label": "Foo.Bar()"},
                {"id": 2, "label": "Baz.Qux()"},
            ]}
        ))

        targets = await mgr.get_step_in_targets()
        assert targets == [
            {"id": 1, "label": "Foo.Bar()"},
            {"id": 2, "label": "Baz.Qux()"},
        ]

    @pytest.mark.asyncio
    async def test_get_step_in_targets_uses_explicit_frame_id(self):
        """get_step_in_targets uses provided frame_id over current_frame_id."""
        mgr = make_manager()
        mgr._state.current_frame_id = 99
        mgr._client.step_in_targets = AsyncMock(return_value=make_dap_response(
            body={"targets": []}
        ))

        await mgr.get_step_in_targets(frame_id=5)
        mgr._client.step_in_targets.assert_called_once_with(5)

    @pytest.mark.asyncio
    async def test_get_step_in_targets_raises_when_no_frame(self):
        """get_step_in_targets raises RuntimeError when no frame available."""
        mgr = make_manager()
        mgr._state.current_frame_id = None

        with pytest.raises(RuntimeError, match="No frame for step-in targets"):
            await mgr.get_step_in_targets()

    @pytest.mark.asyncio
    async def test_get_step_in_targets_returns_empty_on_failure(self):
        """get_step_in_targets returns [] when DAP request fails."""
        mgr = make_manager()
        mgr._state.current_frame_id = 10
        mgr._client.step_in_targets = AsyncMock(
            return_value=make_dap_response(success=False, message="not supported")
        )

        targets = await mgr.get_step_in_targets()
        assert targets == []

    @pytest.mark.asyncio
    async def test_step_in_with_target_id_passes_through(self):
        """step_in passes target_id to DAP client."""
        mgr = make_manager()
        mgr._state.current_thread_id = 1
        mgr._client.step_in = AsyncMock(return_value=make_dap_response())

        await mgr.step_in(target_id=7)
        mgr._client.step_in.assert_called_once_with(1, target_id=7)

    @pytest.mark.asyncio
    async def test_step_in_without_target_id(self):
        """step_in omits target_id when not provided."""
        mgr = make_manager()
        mgr._state.current_thread_id = 1
        mgr._client.step_in = AsyncMock(return_value=make_dap_response())

        await mgr.step_in()
        mgr._client.step_in.assert_called_once_with(1, target_id=None)


# ---------------------------------------------------------------------------
# L3: allThreadsContinued in _on_continued
# ---------------------------------------------------------------------------

class TestOnContinuedAllThreads:
    def test_on_continued_clears_thread_id_when_all_threads_continued(self):
        """_on_continued clears current_thread_id when allThreadsContinued=True."""
        mgr = make_manager()
        mgr._state.state = DebugState.STOPPED
        mgr._state.current_thread_id = 42

        event = DAPEvent(seq=1, event="continued", body={"allThreadsContinued": True})
        mgr._on_continued(event)

        assert mgr._state.current_thread_id is None
        assert mgr._state.state == DebugState.RUNNING

    def test_on_continued_clears_thread_id_when_field_absent(self):
        """_on_continued defaults allThreadsContinued to True when field missing."""
        mgr = make_manager()
        mgr._state.current_thread_id = 42

        event = DAPEvent(seq=1, event="continued", body={})
        mgr._on_continued(event)

        assert mgr._state.current_thread_id is None

    def test_on_continued_preserves_thread_id_when_single_thread(self):
        """_on_continued keeps current_thread_id when allThreadsContinued=False."""
        mgr = make_manager()
        mgr._state.current_thread_id = 42

        event = DAPEvent(seq=1, event="continued", body={"allThreadsContinued": False})
        mgr._on_continued(event)

        assert mgr._state.current_thread_id == 42
        assert mgr._state.state == DebugState.RUNNING

    def test_on_continued_sets_running_state(self):
        """_on_continued transitions state to RUNNING."""
        mgr = make_manager()
        mgr._state.state = DebugState.STOPPED

        event = DAPEvent(seq=1, event="continued", body={})
        mgr._on_continued(event)

        assert mgr._state.state == DebugState.RUNNING


# ---------------------------------------------------------------------------
# L5: Variable paging
# ---------------------------------------------------------------------------

class TestVariablePaging:
    @pytest.mark.asyncio
    async def test_variables_client_passes_filter(self):
        """DAPClient.variables passes filter to DAP."""
        from netcoredbg_mcp.dap.client import DAPClient
        client = DAPClient.__new__(DAPClient)
        client.send_request = AsyncMock(return_value=make_dap_response(body={"variables": []}))

        await client.variables(5, filter="indexed")
        client.send_request.assert_called_once_with(
            Commands.VARIABLES, {"variablesReference": 5, "filter": "indexed"}
        )

    @pytest.mark.asyncio
    async def test_variables_client_passes_paging(self):
        """DAPClient.variables passes start and count to DAP."""
        from netcoredbg_mcp.dap.client import DAPClient
        client = DAPClient.__new__(DAPClient)
        client.send_request = AsyncMock(return_value=make_dap_response(body={"variables": []}))

        await client.variables(5, start=10, count=20)
        client.send_request.assert_called_once_with(
            Commands.VARIABLES, {"variablesReference": 5, "start": 10, "count": 20}
        )

    @pytest.mark.asyncio
    async def test_variables_client_omits_optional_params(self):
        """DAPClient.variables omits optional params when not provided."""
        from netcoredbg_mcp.dap.client import DAPClient
        client = DAPClient.__new__(DAPClient)
        client.send_request = AsyncMock(return_value=make_dap_response(body={"variables": []}))

        await client.variables(5)
        client.send_request.assert_called_once_with(
            Commands.VARIABLES, {"variablesReference": 5}
        )

    @pytest.mark.asyncio
    async def test_session_get_variables_passes_paging(self):
        """SessionManager.get_variables passes filter/start/count to client."""
        mgr = make_manager()
        mgr._client.variables = AsyncMock(
            return_value=make_dap_response(body={"variables": []})
        )

        await mgr.get_variables(5, filter="named", start=0, count=10)
        mgr._client.variables.assert_called_once_with(
            5, filter="named", start=0, count=10
        )

    @pytest.mark.asyncio
    async def test_session_get_variables_no_paging(self):
        """SessionManager.get_variables works without paging params."""
        mgr = make_manager()
        mgr._client.variables = AsyncMock(
            return_value=make_dap_response(
                body={"variables": [{"name": "x", "value": "1", "variablesReference": 0}]}
            )
        )

        result = await mgr.get_variables(5)
        assert len(result) == 1
        assert result[0].name == "x"
        mgr._client.variables.assert_called_once_with(
            5, filter=None, start=None, count=None
        )


# ---------------------------------------------------------------------------
# L6: Output variablesReference
# ---------------------------------------------------------------------------

class TestOutputVariablesReference:
    def test_on_output_stores_variables_reference(self):
        """_on_output stores variablesReference when present and > 0."""
        mgr = make_manager()
        event = DAPEvent(seq=1, event="output", body={
            "category": "console",
            "output": "Object: {x=1}",
            "variablesReference": 42,
        })

        mgr._on_output(event)

        assert len(mgr._state.output_buffer) == 1
        entry = mgr._state.output_buffer[0]
        assert entry.variables_reference == 42
        assert entry.text == "Object: {x=1}"

    def test_on_output_omits_variables_reference_when_zero(self):
        """_on_output leaves variables_reference at 0 when field is 0."""
        mgr = make_manager()
        event = DAPEvent(seq=1, event="output", body={
            "category": "stdout",
            "output": "Hello\n",
            "variablesReference": 0,
        })

        mgr._on_output(event)

        entry = mgr._state.output_buffer[0]
        assert entry.variables_reference == 0

    def test_on_output_omits_variables_reference_when_absent(self):
        """_on_output leaves variables_reference at 0 when field is absent."""
        mgr = make_manager()
        event = DAPEvent(seq=1, event="output", body={
            "category": "stdout",
            "output": "Hello\n",
        })

        mgr._on_output(event)

        entry = mgr._state.output_buffer[0]
        assert entry.variables_reference == 0

    def test_output_entry_default_variables_reference(self):
        """OutputEntry.variables_reference defaults to 0."""
        entry = OutputEntry(text="hi")
        assert entry.variables_reference == 0

    def test_output_entry_with_variables_reference(self):
        """OutputEntry accepts non-zero variables_reference."""
        entry = OutputEntry(text="obj", category="console", variables_reference=7)
        assert entry.variables_reference == 7
