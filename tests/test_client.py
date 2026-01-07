"""Tests for DAP client."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from netcoredbg_mcp.dap.client import DAPClient
from netcoredbg_mcp.dap.protocol import DAPResponse, DAPEvent


class TestDAPClientInit:
    """Tests for DAPClient initialization."""

    def test_init_with_path(self):
        """Test initialization with custom path."""
        client = DAPClient("/custom/netcoredbg")

        assert client.netcoredbg_path == "/custom/netcoredbg"

    def test_init_from_env(self):
        """Test initialization from environment variable."""
        with patch.dict("os.environ", {"NETCOREDBG_PATH": "/env/netcoredbg"}):
            with patch("os.path.exists", return_value=True):
                client = DAPClient()
                assert client.netcoredbg_path == "/env/netcoredbg"

    def test_init_state(self):
        """Test initial state after init."""
        client = DAPClient("/path/to/netcoredbg")

        assert client._seq == 0
        assert client._pending == {}
        assert client._event_handlers == {}
        assert client._process is None
        assert client._capabilities == {}


class TestDAPClientProperties:
    """Tests for DAPClient properties."""

    def test_is_running_false_when_no_process(self):
        """Test is_running is False when no process."""
        client = DAPClient("/path")
        assert not client.is_running

    def test_is_running_false_when_process_terminated(self):
        """Test is_running is False when process terminated."""
        client = DAPClient("/path")
        mock_process = MagicMock()
        mock_process.returncode = 0  # Terminated
        client._process = mock_process

        assert not client.is_running

    def test_is_running_true_when_process_active(self):
        """Test is_running is True when process is active."""
        client = DAPClient("/path")
        mock_process = MagicMock()
        mock_process.returncode = None  # Still running
        client._process = mock_process

        assert client.is_running


class TestDAPClientEventHandlers:
    """Tests for event handler registration."""

    def test_on_event_registers_handler(self):
        """Test registering event handler."""
        client = DAPClient("/path")
        handler = MagicMock()

        client.on_event("stopped", handler)

        assert "stopped" in client._event_handlers
        assert handler in client._event_handlers["stopped"]

    def test_on_event_multiple_handlers(self):
        """Test registering multiple handlers for same event."""
        client = DAPClient("/path")
        handler1 = MagicMock()
        handler2 = MagicMock()

        client.on_event("stopped", handler1)
        client.on_event("stopped", handler2)

        assert len(client._event_handlers["stopped"]) == 2

    def test_off_event_removes_handler(self):
        """Test unregistering event handler."""
        client = DAPClient("/path")
        handler = MagicMock()
        client.on_event("stopped", handler)

        client.off_event("stopped", handler)

        assert handler not in client._event_handlers["stopped"]

    def test_handle_message_calls_event_handlers(self):
        """Test that _handle_message calls registered handlers."""
        client = DAPClient("/path")
        handler = MagicMock()
        client.on_event("output", handler)

        # Simulate receiving an event
        data = {
            "seq": 1,
            "type": "event",
            "event": "output",
            "body": {"output": "test"}
        }
        client._handle_message(data)

        handler.assert_called_once()
        call_arg = handler.call_args[0][0]
        assert isinstance(call_arg, DAPEvent)
        assert call_arg.event == "output"

    def test_handle_message_handles_handler_exception(self):
        """Test that handler exceptions don't crash client."""
        client = DAPClient("/path")
        handler = MagicMock(side_effect=Exception("Handler error"))
        client.on_event("stopped", handler)

        data = {"seq": 1, "type": "event", "event": "stopped", "body": {}}
        # Should not raise
        client._handle_message(data)


class TestDAPClientResponseHandling:
    """Tests for response handling."""

    @pytest.mark.asyncio
    async def test_handle_message_resolves_pending_future(self):
        """Test that _handle_message resolves pending request futures."""
        client = DAPClient("/path")

        # Create a pending future in the current event loop
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        client._pending[1] = future

        # Simulate receiving a response
        data = {
            "seq": 1,
            "type": "response",
            "request_seq": 1,
            "success": True,
            "command": "threads",
            "body": {"threads": []}
        }
        client._handle_message(data)

        assert future.done()
        result = future.result()
        assert isinstance(result, DAPResponse)
        assert result.success is True


class TestDAPClientRequestBuilding:
    """Tests for building DAP requests."""

    @pytest.mark.asyncio
    async def test_initialize_request_format(self):
        """Test initialize request has correct format."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(
                seq=1, request_seq=1, success=True, command=command,
                body={"supportsConfigurationDoneRequest": True}
            )

        client.send_request = mock_send
        await client.initialize()

        assert captured_args["command"] == "initialize"
        assert "adapterID" in captured_args["arguments"]
        assert captured_args["arguments"]["adapterID"] == "coreclr"
        assert captured_args["arguments"]["clientID"] == "netcoredbg-mcp"

    @pytest.mark.asyncio
    async def test_launch_request_format(self):
        """Test launch request has correct format."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.launch(
            program="test.dll",
            cwd="/test",
            args=["--arg1"],
            stop_at_entry=True
        )

        assert captured_args["command"] == "launch"
        assert captured_args["arguments"]["program"] == "test.dll"
        assert captured_args["arguments"]["cwd"] == "/test"
        assert captured_args["arguments"]["args"] == ["--arg1"]
        assert captured_args["arguments"]["stopAtEntry"] is True

    @pytest.mark.asyncio
    async def test_set_breakpoints_request_format(self):
        """Test setBreakpoints request has correct format."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(
                seq=1, request_seq=1, success=True, command=command,
                body={"breakpoints": [{"verified": True, "line": 10}]}
            )

        client.send_request = mock_send
        await client.set_breakpoints(
            source_path="test.cs",
            breakpoints=[{"line": 10, "condition": "x > 5"}]
        )

        assert captured_args["command"] == "setBreakpoints"
        assert captured_args["arguments"]["source"]["path"] == "test.cs"
        assert len(captured_args["arguments"]["breakpoints"]) == 1
        assert captured_args["arguments"]["breakpoints"][0]["line"] == 10

    @pytest.mark.asyncio
    async def test_continue_request_format(self):
        """Test continue request has correct format."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.continue_execution(thread_id=1)

        assert captured_args["command"] == "continue"
        assert captured_args["arguments"]["threadId"] == 1

    @pytest.mark.asyncio
    async def test_stack_trace_request_format(self):
        """Test stackTrace request has correct format."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(
                seq=1, request_seq=1, success=True, command=command,
                body={"stackFrames": []}
            )

        client.send_request = mock_send
        await client.stack_trace(thread_id=1, start_frame=0, levels=20)

        assert captured_args["command"] == "stackTrace"
        assert captured_args["arguments"]["threadId"] == 1
        assert captured_args["arguments"]["startFrame"] == 0
        assert captured_args["arguments"]["levels"] == 20

    @pytest.mark.asyncio
    async def test_evaluate_request_format(self):
        """Test evaluate request has correct format."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(
                seq=1, request_seq=1, success=True, command=command,
                body={"result": "10", "type": "int"}
            )

        client.send_request = mock_send
        await client.evaluate("x + y", frame_id=0, context="watch")

        assert captured_args["command"] == "evaluate"
        assert captured_args["arguments"]["expression"] == "x + y"
        assert captured_args["arguments"]["frameId"] == 0
        assert captured_args["arguments"]["context"] == "watch"


class TestDAPClientStepCommands:
    """Tests for stepping commands."""

    @pytest.mark.asyncio
    async def test_step_over(self):
        """Test step over command."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.step_over(thread_id=1)

        assert captured_args["command"] == "next"
        assert captured_args["arguments"]["threadId"] == 1

    @pytest.mark.asyncio
    async def test_step_in(self):
        """Test step into command."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.step_in(thread_id=1)

        assert captured_args["command"] == "stepIn"

    @pytest.mark.asyncio
    async def test_step_out(self):
        """Test step out command."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.step_out(thread_id=1)

        assert captured_args["command"] == "stepOut"

    @pytest.mark.asyncio
    async def test_pause(self):
        """Test pause command."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.pause(thread_id=1)

        assert captured_args["command"] == "pause"
        assert captured_args["arguments"]["threadId"] == 1


class TestDAPClientVariableInspection:
    """Tests for variable inspection commands."""

    @pytest.mark.asyncio
    async def test_scopes_request(self):
        """Test scopes request."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(
                seq=1, request_seq=1, success=True, command=command,
                body={"scopes": [{"name": "Locals", "variablesReference": 1}]}
            )

        client.send_request = mock_send
        await client.scopes(frame_id=0)

        assert captured_args["command"] == "scopes"
        assert captured_args["arguments"]["frameId"] == 0

    @pytest.mark.asyncio
    async def test_variables_request(self):
        """Test variables request."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(
                seq=1, request_seq=1, success=True, command=command,
                body={"variables": []}
            )

        client.send_request = mock_send
        await client.variables(variables_reference=1)

        assert captured_args["command"] == "variables"
        assert captured_args["arguments"]["variablesReference"] == 1

    @pytest.mark.asyncio
    async def test_threads_request(self):
        """Test threads request."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(
                seq=1, request_seq=1, success=True, command=command,
                body={"threads": [{"id": 1, "name": "Main"}]}
            )

        client.send_request = mock_send
        await client.threads()

        assert captured_args["command"] == "threads"


class TestDAPClientDisconnect:
    """Tests for disconnect operations."""

    @pytest.mark.asyncio
    async def test_disconnect_with_terminate(self):
        """Test disconnect with terminate."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.disconnect(terminate=True)

        assert captured_args["command"] == "disconnect"
        assert captured_args["arguments"]["terminateDebuggee"] is True

    @pytest.mark.asyncio
    async def test_disconnect_without_terminate(self):
        """Test disconnect without terminate."""
        client = DAPClient("/path")

        captured_args = {}
        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.disconnect(terminate=False)

        assert captured_args["arguments"]["terminateDebuggee"] is False
