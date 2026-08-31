"""Tests for DAP client."""

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from netcoredbg_mcp.dap.client import (
    DapCleanupOutcome,
    DAPClient,
    DapTransportTerminal,
    sanitize_terminal_text,
)
from netcoredbg_mcp.dap.protocol import Commands, DAPEvent, DAPRequest, DAPResponse
from netcoredbg_mcp.resource_updates import STATE_URI, THREADS_URI
from netcoredbg_mcp.session import DebugState, SessionManager
from netcoredbg_mcp.windows_process_owner import DrainStatus, OwnedProcessRef, OwnerDrainReceipt
from tests.owner_scope_red import BlockingStream, TreeProcess


class OwnedTestProcess:
    """A test double for the private Windows owner capability."""

    def __init__(self, process: Any, generation: object = "test") -> None:
        self._process = process
        self.owner = OwnedProcessRef("test-owner", generation, process.pid)

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def stdin(self) -> Any:
        return getattr(self._process, "stdin", None)

    @property
    def stdout(self) -> Any:
        return self._process.stdout

    @property
    def stderr(self) -> Any:
        return self._process.stderr

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait(self) -> int | None:
        return await self._process.wait()

    async def drain_after_grace(
        self,
        *,
        grace_timeout: float,
        force_timeout: float,
    ) -> OwnerDrainReceipt:
        forced = False
        if self._process.returncode is None:
            try:
                await asyncio.wait_for(self._process.wait(), grace_timeout)
            except asyncio.TimeoutError:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), force_timeout)
                except asyncio.TimeoutError:
                    forced = True
                    try:
                        self._process.kill()
                    except ProcessLookupError:
                        # The root disappeared between the grace deadline and
                        # the force attempt. A real owner observes zero Job
                        # accounting before TerminateJobObject and therefore
                        # records this as a natural drain, not a forced kill.
                        forced = False
                    else:
                        try:
                            await asyncio.wait_for(self._process.wait(), force_timeout)
                        except asyncio.TimeoutError:
                            pass
        if hasattr(self._process, "child_alive"):
            self._process.child_alive = False
        return OwnerDrainReceipt(
            owner=self.owner,
            status=DrainStatus.DRAINED,
            forced=forced,
            root_returncode=self._process.returncode,
            active_processes=0,
            root_was_forced=forced,
        )

    async def aclose(self) -> None:
        return None


class TestDAPClientInit:
    """Tests for DAPClient initialization."""

    def test_init_with_path(self):
        """Test initialization with custom path."""
        client = DAPClient("/custom/netcoredbg")

        assert client.netcoredbg_path == "/custom/netcoredbg"

    def test_init_from_env(self):
        """Test initialization from environment variable."""
        with patch.dict("os.environ", {"NETCOREDBG_PATH": "/env/netcoredbg"}):
            with patch("os.path.isfile", return_value=True):
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

    def test_update_capabilities_shallow_merges_delta(self):
        """Capabilities event deltas merge through the public client API."""
        client = DAPClient("/path")
        client._capabilities = {
            "supportsDisassembleRequest": False,
            "supportsStepInTargetsRequest": True,
        }

        added, changed, total_before, total_after = client.update_capabilities(
            {
                "supportsDisassembleRequest": True,
                "supportsLoadedSourcesRequest": True,
            }
        )

        assert added == ["supportsLoadedSourcesRequest"]
        assert changed == [
            "supportsDisassembleRequest",
            "supportsLoadedSourcesRequest",
        ]
        assert total_before == 2
        assert total_after == 3
        assert client.capabilities == {
            "supportsDisassembleRequest": True,
            "supportsStepInTargetsRequest": True,
            "supportsLoadedSourcesRequest": True,
        }


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
        data = {"seq": 1, "type": "event", "event": "output", "body": {"output": "test"}}
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
            "body": {"threads": []},
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
                seq=1,
                request_seq=1,
                success=True,
                command=command,
                body={"supportsConfigurationDoneRequest": True},
            )

        client.send_request = mock_send
        await client.initialize()

        assert captured_args["command"] == "initialize"
        assert "adapterID" in captured_args["arguments"]
        assert captured_args["arguments"]["adapterID"] == "coreclr"
        assert captured_args["arguments"]["clientID"] == "netcoredbg-mcp"
        assert captured_args["arguments"]["supportsProgressReporting"] is True
        assert captured_args["arguments"]["supportsMemoryReferences"] is True

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
        await client.launch(program="test.dll", cwd="/test", args=["--arg1"], stop_at_entry=True)

        assert captured_args["command"] == "launch"
        assert captured_args["arguments"]["program"] == "test.dll"
        assert captured_args["arguments"]["cwd"] == "/test"
        assert captured_args["arguments"]["args"] == ["--arg1"]
        assert captured_args["arguments"]["stopAtEntry"] is True

    @pytest.mark.asyncio
    async def test_launch_request_preserves_env_null_values(self):
        """Test launch env preserves DAP null removal semantics."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        with patch.dict("os.environ", {"PATH": "base-path"}, clear=True):
            await client.launch(
                program="test.dll",
                env={"APP_MODE": "debug", "REMOVE_ME": None},
            )

        assert captured_args["command"] == "launch"
        assert captured_args["arguments"]["env"]["PATH"] == "base-path"
        assert captured_args["arguments"]["env"]["APP_MODE"] == "debug"
        assert captured_args["arguments"]["env"]["REMOVE_ME"] is None

    @pytest.mark.asyncio
    async def test_launch_inherits_process_environment_by_default(self):
        """Launch carries inherited env so Windows GUI apps get system variables."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        with patch("netcoredbg_mcp.dap.client.os.name", "nt"):
            with patch.dict(
                "os.environ",
                {
                    "WINDIR": r"C:\WINDOWS",
                    "SystemRoot": r"C:\WINDOWS",
                    "USERPROFILE": r"C:\Users\tester",
                    "PATH": r"C:\WINDOWS\system32",
                    "TEMP": r"C:\Temp",
                    "TMP": r"C:\Temp",
                },
                clear=True,
            ):
                await client.launch(program="test.dll", cwd="/test")

        env = captured_args["arguments"]["env"]
        assert env["WINDIR"] == r"C:\WINDOWS"
        assert env["SYSTEMROOT"] == r"C:\WINDOWS"
        assert env["USERPROFILE"] == r"C:\Users\tester"
        assert env["PATH"] == r"C:\WINDOWS\system32"
        assert env["TEMP"] == r"C:\Temp"
        assert env["TMP"] == r"C:\Temp"

    @pytest.mark.asyncio
    async def test_launch_env_overrides_inherited_values(self):
        """Caller env remains an override layer on top of inherited env."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        with patch.dict("os.environ", {"APP_MODE": "server", "PATH": "base"}, clear=True):
            await client.launch(
                program="test.dll",
                cwd="/test",
                env={"APP_MODE": "debug", "EXTRA": "1"},
            )

        env = captured_args["arguments"]["env"]
        assert env["PATH"] == "base"
        assert env["APP_MODE"] == "debug"
        assert env["EXTRA"] == "1"

    @pytest.mark.asyncio
    async def test_windows_launch_env_overrides_case_variant_path(self):
        """Windows env overrides replace inherited keys regardless of casing."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        with patch("netcoredbg_mcp.dap.client.os.name", "nt"):
            with patch.dict("os.environ", {"Path": "base", "OTHER": "x"}, clear=True):
                await client.launch(
                    program="test.dll",
                    cwd="/test",
                    env={"PATH": "debug", "EXTRA": "1"},
                )

        env = captured_args["arguments"]["env"]
        assert env["PATH"] == "debug"
        assert env["EXTRA"] == "1"
        assert "Path" not in env

    @pytest.mark.asyncio
    async def test_windows_launch_env_normalizes_alias_overrides(self):
        """Windows env aliases must not defeat explicit overrides by case/order."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        with patch("netcoredbg_mcp.dap.client.os.name", "nt"):
            with patch.dict(
                "os.environ",
                {
                    "windir": r"C:\WINDOWS",
                    "SystemRoot": r"C:\WINDOWS",
                },
                clear=True,
            ):
                await client.launch(program="test.dll", env={"WINDIR": r"D:\Windows"})

        env = captured_args["arguments"]["env"]
        assert env["WINDIR"] == r"D:\Windows"
        assert env["SYSTEMROOT"] == r"D:\Windows"
        assert "windir" not in env
        assert "SystemRoot" not in env

    @pytest.mark.asyncio
    async def test_windows_launch_env_preserves_alias_unset_overrides(self):
        """Windows env alias repair must preserve explicit DAP null removals."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        with patch("netcoredbg_mcp.dap.client.os.name", "nt"):
            with patch.dict(
                "os.environ",
                {
                    "WINDIR": r"C:\WINDOWS",
                    "SystemRoot": r"C:\WINDOWS",
                },
                clear=True,
            ):
                await client.launch(
                    program="test.dll",
                    env={"WINDIR": None, "SystemRoot": None},
                )

        env = captured_args["arguments"]["env"]
        assert env["WINDIR"] is None
        assert env["SYSTEMROOT"] is None

    @pytest.mark.asyncio
    async def test_windows_launch_env_preserves_empty_string_alias_overrides(self):
        """Windows env alias repair must preserve explicit empty-string overrides."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        with patch("netcoredbg_mcp.dap.client.os.name", "nt"):
            with patch.dict(
                "os.environ",
                {
                    "WINDIR": r"C:\WINDOWS",
                    "SystemRoot": r"C:\WINDOWS",
                },
                clear=True,
            ):
                await client.launch(program="test.dll", env={"WINDIR": ""})

        env = captured_args["arguments"]["env"]
        assert env["WINDIR"] == ""
        assert env["SYSTEMROOT"] == ""

    @pytest.mark.asyncio
    async def test_send_redacts_launch_env_values_in_logs(self, caplog):
        """Test launch request logging redacts env values without changing the request."""

        class FakeStdin:
            def __init__(self):
                self.data = b""

            def write(self, data):
                self.data += data

            async def drain(self):
                return None

        client = DAPClient("/path")
        client._process = MagicMock()
        client._process.stdin = FakeStdin()
        request = DAPRequest(
            seq=1,
            command=Commands.LAUNCH,
            arguments={
                "program": "test.dll",
                "env": {"APP_SECRET": "secret-value", "REMOVE_ME": None},
            },
        )

        with caplog.at_level(logging.DEBUG, logger="netcoredbg_mcp.dap.client"):
            await client._send(request)

        assert "secret-value" not in caplog.text
        assert "APP_SECRET" not in caplog.text
        assert "REMOVE_ME" not in caplog.text
        assert "<2 environment variables>" in caplog.text
        assert request.arguments["env"] == {"APP_SECRET": "secret-value", "REMOVE_ME": None}

    @pytest.mark.asyncio
    async def test_set_breakpoints_request_format(self):
        """Test setBreakpoints request has correct format."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(
                seq=1,
                request_seq=1,
                success=True,
                command=command,
                body={"breakpoints": [{"verified": True, "line": 10}]},
            )

        client.send_request = mock_send
        await client.set_breakpoints(
            source_path="test.cs", breakpoints=[{"line": 10, "condition": "x > 5"}]
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
                seq=1, request_seq=1, success=True, command=command, body={"stackFrames": []}
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
                seq=1,
                request_seq=1,
                success=True,
                command=command,
                body={"result": "10", "type": "int"},
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
                seq=1,
                request_seq=1,
                success=True,
                command=command,
                body={"scopes": [{"name": "Locals", "variablesReference": 1}]},
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
                seq=1, request_seq=1, success=True, command=command, body={"variables": []}
            )

        client.send_request = mock_send
        await client.variables(variables_reference=1)

        assert captured_args["command"] == "variables"
        assert captured_args["arguments"]["variablesReference"] == 1

    @pytest.mark.asyncio
    async def test_read_memory_request(self):
        """Test readMemory request."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.read_memory("0x1234", offset=4, count=16)

        assert captured_args["command"] == "readMemory"
        assert captured_args["arguments"] == {
            "memoryReference": "0x1234",
            "offset": 4,
            "count": 16,
        }

    @pytest.mark.asyncio
    async def test_write_memory_request(self):
        """Test writeMemory request."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.write_memory("0x1234", "AQID", offset=2, allow_partial=True)

        assert captured_args["command"] == "writeMemory"
        assert captured_args["arguments"] == {
            "memoryReference": "0x1234",
            "offset": 2,
            "data": "AQID",
            "allowPartial": True,
        }

    @pytest.mark.asyncio
    async def test_loaded_sources_request(self):
        """Test loadedSources request."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.loaded_sources()

        assert captured_args["command"] == "loadedSources"
        assert captured_args["arguments"] is None

    @pytest.mark.asyncio
    async def test_disassemble_request(self):
        """Test disassemble request."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.disassemble(
            "0x1234",
            offset=4,
            instruction_offset=-2,
            instruction_count=8,
            resolve_symbols=False,
        )

        assert captured_args["command"] == "disassemble"
        assert captured_args["arguments"] == {
            "memoryReference": "0x1234",
            "offset": 4,
            "instructionOffset": -2,
            "instructionCount": 8,
            "resolveSymbols": False,
        }

    @pytest.mark.asyncio
    async def test_locations_request(self):
        """Test locations request."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(seq=1, request_seq=1, success=True, command=command)

        client.send_request = mock_send
        await client.locations(42)

        assert captured_args["command"] == "locations"
        assert captured_args["arguments"] == {"locationReference": 42}

    @pytest.mark.asyncio
    async def test_threads_request(self):
        """Test threads request."""
        client = DAPClient("/path")

        captured_args = {}

        async def mock_send(command, arguments=None, timeout=30.0):
            captured_args["command"] = command
            captured_args["arguments"] = arguments
            return DAPResponse(
                seq=1,
                request_seq=1,
                success=True,
                command=command,
                body={"threads": [{"id": 1, "name": "Main"}]},
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


class TestDAPClientTransportDeath:
    """Regression coverage for adapter transport loss without DAP termination."""

    @pytest.mark.asyncio
    async def test_stdout_eof_publishes_terminal_manager_state(self):
        """Raw adapter EOF must invalidate the manager's public live-session state."""

        class ClosedStdout:
            """Minimal stream whose first read observes a closed adapter pipe."""

            async def readline(self) -> bytes:
                return b""

        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            manager = SessionManager()

        client = DAPClient("/path")
        manager._client = client
        manager._register_event_handlers()
        manager._state.state = DebugState.RUNNING
        manager._state.process_id = 29736

        state_changes: list[DebugState] = []
        resource_updates: list[tuple[str, ...]] = []
        manager.on_state_change(state_changes.append)

        async def record_resource_updates(uris: tuple[str, ...]) -> None:
            resource_updates.append(uris)

        manager.set_resource_update_callback(record_resource_updates)

        pending = asyncio.get_running_loop().create_future()
        client._pending[1] = pending
        client._process = SimpleNamespace(pid=41000, stdout=ClosedStdout(), returncode=1)

        try:
            await client._read_loop()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            state = manager.state.to_dict()
            observed = {
                "state": state["state"],
                "debuggeeAlive": state["debuggeeAlive"],
                "stateChanges": state_changes,
                "resourceUpdates": resource_updates,
                "pendingError": str(pending.exception()),
            }
        finally:
            await manager.close_resource_update_notifications()

        assert observed == {
            "state": DebugState.TERMINATED.value,
            "debuggeeAlive": False,
            "stateChanges": [DebugState.TERMINATED],
            "resourceUpdates": [(STATE_URI,), (THREADS_URI,)],
            "pendingError": "netcoredbg process died — pending request cancelled",
        }

    @pytest.mark.asyncio
    async def test_start_owns_stdout_stderr_and_process_observers(self):
        """Client launch must independently begin stdout, stderr, and process observation."""

        class BlockingStream:
            def __init__(self) -> None:
                self.read_started = asyncio.Event()
                self.release = asyncio.Event()

            async def readline(self) -> bytes:
                self.read_started.set()
                await self.release.wait()
                return b""

            async def read(self, _size: int = -1) -> bytes:
                self.read_started.set()
                await self.release.wait()
                return b""

        class BlockingProcess:
            def __init__(
                self,
                stdout: BlockingStream,
                stderr: BlockingStream,
            ) -> None:
                self.pid = 41001
                self.stdout = stdout
                self.stderr = stderr
                self.returncode: int | None = None
                self.wait_started = asyncio.Event()
                self.wait_release = asyncio.Event()

            async def wait(self) -> int:
                self.wait_started.set()
                await self.wait_release.wait()
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def terminate(self) -> None:
                self.returncode = 0
                self.wait_release.set()

            def kill(self) -> None:
                self.returncode = -9
                self.wait_release.set()

        stdout = BlockingStream()
        stderr = BlockingStream()
        process = BlockingProcess(stdout, stderr)
        client = DAPClient("/path")

        try:
            with patch(
                "netcoredbg_mcp.dap.client.WindowsOwnedProcess.launch",
                return_value=OwnedTestProcess(process),
            ):
                await client.start()

            for _ in range(3):
                await asyncio.sleep(0)

            observed = (
                stdout.read_started.is_set(),
                stderr.read_started.is_set(),
                process.wait_started.is_set(),
            )
        finally:
            stdout.release.set()
            stderr.release.set()
            process.wait_release.set()
            if client._read_task:
                await client._read_task

        assert observed == (True, True, True)

    @pytest.mark.asyncio
    async def test_eof_drains_exited_adapter_without_second_terminate_and_settles_pending(
        self,
    ):
        """EOF must drain stderr, join an exited adapter, and settle pending work."""

        class GatedEofStdout:
            def __init__(self) -> None:
                self.read_started = asyncio.Event()
                self.release = asyncio.Event()

            async def readline(self) -> bytes:
                self.read_started.set()
                await self.release.wait()
                return b""

        class EmptyStderr:
            def __init__(self) -> None:
                self.read_calls = 0

            async def read(self, _size: int = -1) -> bytes:
                self.read_calls += 1
                return b""

            async def readline(self) -> bytes:
                return await self.read()

        class AlreadyExitedProcess:
            def __init__(
                self,
                stdout: GatedEofStdout,
                stderr: EmptyStderr,
            ) -> None:
                self.pid = 41002
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = 23
                self.wait_calls = 0
                self.wait_release = asyncio.Event()
                self.terminate_calls = 0

            async def wait(self) -> int:
                self.wait_calls += 1
                await self.wait_release.wait()
                return self.returncode

            def terminate(self) -> None:
                self.terminate_calls += 1

            def kill(self) -> None:
                self.terminate_calls += 1

        stdout = GatedEofStdout()
        stderr = EmptyStderr()
        process = AlreadyExitedProcess(stdout, stderr)
        client = DAPClient("/path")

        try:
            with patch(
                "netcoredbg_mcp.dap.client.WindowsOwnedProcess.launch",
                return_value=OwnedTestProcess(process),
            ):
                await client.start()

            for _ in range(3):
                await asyncio.sleep(0)

            pending = asyncio.get_running_loop().create_future()
            client._pending[1] = pending
            stdout.release.set()
            process.wait_release.set()
            assert client._read_task is not None
            await client._read_task

            for _ in range(3):
                await asyncio.sleep(0)

            observed = (
                stderr.read_calls > 0,
                process.wait_calls > 0,
                process.terminate_calls == 0,
                str(pending.exception()),
            )
        finally:
            stdout.release.set()
            process.wait_release.set()

        assert observed == (
            True,
            True,
            True,
            "netcoredbg process died — pending request cancelled",
        )

    @pytest.mark.asyncio
    async def test_kill_disappearance_race_still_publishes_terminal_record(self):
        """A process disappearing during kill escalation must not abort finalization."""

        class ClosedStdout:
            async def readline(self) -> bytes:
                return b""

        class EmptyStderr:
            async def read(self, _size: int = -1) -> bytes:
                return b""

            async def readline(self) -> bytes:
                return b""

        class VanishingProcess:
            def __init__(self) -> None:
                self.pid = 41009
                self.stdout = ClosedStdout()
                self.stderr = EmptyStderr()
                self.returncode: int | None = None
                self.release = asyncio.Event()
                self.terminate_calls = 0
                self.kill_calls = 0

            async def wait(self) -> int:
                await self.release.wait()
                return 23

            def terminate(self) -> None:
                self.terminate_calls += 1

            def kill(self) -> None:
                self.kill_calls += 1
                raise ProcessLookupError

        process = VanishingProcess()
        client = DAPClient("/path")
        records: list[DapTransportTerminal] = []
        published = asyncio.Event()

        def record(terminal: DapTransportTerminal) -> None:
            records.append(terminal)
            published.set()

        client.set_transport_terminal_handler(record)

        try:
            with (
                patch(
                    "netcoredbg_mcp.dap.client.WindowsOwnedProcess.launch",
                    return_value=OwnedTestProcess(process),
                ),
                patch("netcoredbg_mcp.dap.client.NATURAL_EXIT_TIMEOUT", 0.01),
                patch("netcoredbg_mcp.dap.client.TERMINATE_TIMEOUT", 0.01),
                patch("netcoredbg_mcp.dap.client.KILL_TIMEOUT", 0.01),
            ):
                await client.start(generation=1)
                await asyncio.wait_for(published.wait(), timeout=1.0)
        finally:
            process.release.set()
            run = client._run
            if run is not None and run.process_task is not None:
                await run.process_task

        assert len(records) == 1
        assert records[0].process_exited is True
        assert records[0].cleanup_outcome is DapCleanupOutcome.NATURAL_EXIT
        assert process.terminate_calls == 1
        assert process.kill_calls == 1

    @pytest.mark.asyncio
    async def test_terminal_callback_preserves_immutable_known_and_unobserved_exit_facts(
        self,
    ):
        """One callback must preserve bounded facts without conflating DAP and process exit."""

        class ScriptedStdout:
            def __init__(
                self,
                lines: list[bytes] | None = None,
                content: bytes = b"",
                failure: Exception | None = None,
            ) -> None:
                self._lines = list(lines or [])
                self._content = content
                self._failure = failure

            async def readline(self) -> bytes:
                if self._failure is not None:
                    failure = self._failure
                    self._failure = None
                    raise failure
                return self._lines.pop(0) if self._lines else b""

            async def readexactly(self, size: int) -> bytes:
                assert size == len(self._content)
                return self._content

        class ScriptedStderr:
            def __init__(self, chunks: list[bytes]) -> None:
                self._chunks = list(chunks)

            async def read(self, _size: int = -1) -> bytes:
                return self._chunks.pop(0) if self._chunks else b""

            async def readline(self) -> bytes:
                return await self.read()

        class CompletedProcess:
            def __init__(
                self,
                pid: int,
                stdout: ScriptedStdout,
                stderr: ScriptedStderr,
                returncode: int | None,
                wait_result: int | None,
            ) -> None:
                self.pid = pid
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode
                self._wait_result = wait_result

            async def wait(self) -> int | None:
                return self._wait_result

            def terminate(self) -> None:
                pass

            def kill(self) -> None:
                pass

        async def wait_for_record(records: list[Any]) -> None:
            for _ in range(20):
                if records:
                    return
                await asyncio.sleep(0)
            pytest.fail("transport terminal callback did not publish a record")

        async def start_with_recorder(
            process: CompletedProcess,
            generation: object,
            records: list[Any],
        ) -> tuple[DAPClient, Any]:
            client = DAPClient("/path")
            set_terminal_handler = getattr(client, "set_transport_terminal_handler", None)
            assert callable(set_terminal_handler), (
                "DAPClient must expose the manager terminal callback seam"
            )
            set_terminal_handler(records.append)
            with patch(
                "netcoredbg_mcp.dap.client.WindowsOwnedProcess.launch",
                return_value=OwnedTestProcess(process, generation),
            ):
                returned_generation = await client.start(generation=generation)
            await wait_for_record(records)
            return client, returned_generation

        event_body = {"output": "event-marker-" + "e" * 8192}
        event_content = json.dumps(
            {
                "seq": 41,
                "type": "event",
                "event": "output",
                "body": event_body,
            },
            separators=(",", ":"),
        ).encode()
        stderr_tail_marker = b"stderr-tail-marker"
        stderr_payload = b"discarded-stderr-" * 2048 + stderr_tail_marker
        known_records: list[Any] = []
        known_generation = object()
        known_process = CompletedProcess(
            41003,
            ScriptedStdout(
                [
                    f"Content-Length: {len(event_content)}\r\n".encode(),
                    b"\r\n",
                    b"",
                ],
                event_content,
            ),
            ScriptedStderr([stderr_payload, b""]),
            returncode=23,
            wait_result=23,
        )
        known_client, returned_generation = await start_with_recorder(
            known_process,
            known_generation,
            known_records,
        )

        assert returned_generation is known_generation
        assert len(known_records) == 1
        known = known_records[0]
        assert known.generation is known_generation
        assert known.adapter_pid == 41003
        assert known.process_exited is True
        assert known.returncode == 23
        assert known.stdout_eof is True
        assert known.last_dap_event == (41, "output")
        assert "event-marker" in str(known.last_dap_event_body_preview)
        assert len(str(known.last_dap_event_body_preview)) < len(event_body["output"])
        stderr_tail = known.stderr_tail
        assert (
            stderr_tail_marker in stderr_tail
            if isinstance(stderr_tail, bytes)
            else stderr_tail_marker.decode() in stderr_tail
        )
        assert len(stderr_tail) < len(stderr_payload)
        assert known.stderr_truncated is True
        assert known.reader_error is None

        known_projection = (
            known.generation,
            known.adapter_pid,
            known.process_exited,
            known.returncode,
            known.last_dap_event,
            known.last_dap_event_body_preview,
            known.stderr_tail,
            known.stderr_truncated,
            known.reader_error,
        )
        known_client._handle_message(
            {
                "seq": 42,
                "type": "event",
                "event": "output",
                "body": {"output": "late-event-must-not-mutate-terminal-record"},
            }
        )
        await asyncio.sleep(0)
        assert known_records == [known]
        assert (
            known.generation,
            known.adapter_pid,
            known.process_exited,
            known.returncode,
            known.last_dap_event,
            known.last_dap_event_body_preview,
            known.stderr_tail,
            known.stderr_truncated,
            known.reader_error,
        ) == known_projection

        reader_message = "reader-marker-" + "r" * 8192
        unknown_records: list[Any] = []
        unknown_generation = object()
        _, unknown_returned_generation = await start_with_recorder(
            CompletedProcess(
                41004,
                ScriptedStdout(failure=ValueError(reader_message)),
                ScriptedStderr([b""]),
                returncode=None,
                wait_result=None,
            ),
            unknown_generation,
            unknown_records,
        )

        assert unknown_returned_generation is unknown_generation
        assert len(unknown_records) == 1
        unknown = unknown_records[0]
        assert unknown.generation is unknown_generation
        assert unknown.adapter_pid == 41004
        assert unknown.process_exited is True
        assert unknown.returncode is None
        assert unknown.stdout_eof is False
        assert unknown.last_dap_event is None
        assert unknown.reader_error is not None
        assert "ValueError" in str(unknown.reader_error)
        assert len(str(unknown.reader_error)) < len(reader_message)

    @pytest.mark.asyncio
    async def test_dap_terminated_then_eof_and_process_exit_publish_once(self):
        """Protocol termination, EOF, and process exit must share one finalizer."""

        payload = json.dumps(
            {"seq": 51, "type": "event", "event": "terminated", "body": {}}
        ).encode("utf-8")
        stdout = MagicMock()
        stdout.readline = AsyncMock(
            side_effect=[
                f"Content-Length: {len(payload)}\r\n".encode("ascii"),
                b"\r\n",
                b"",
            ]
        )
        stdout.readexactly = AsyncMock(return_value=payload)
        stderr = MagicMock()
        stderr.read = AsyncMock(return_value=b"")
        process = MagicMock(pid=41005, stdout=stdout, stderr=stderr, returncode=0)
        process.wait = AsyncMock(return_value=0)

        records: list[Any] = []
        client = DAPClient("/path")
        set_terminal_handler = getattr(client, "set_transport_terminal_handler", None)
        assert callable(set_terminal_handler), (
            "DAPClient must expose the manager terminal callback seam"
        )
        set_terminal_handler(records.append)

        generation = object()
        with patch(
            "netcoredbg_mcp.dap.client.WindowsOwnedProcess.launch",
            return_value=OwnedTestProcess(process, generation),
        ):
            returned_generation = await client.start(generation=generation)

        for _ in range(20):
            if records:
                break
            await asyncio.sleep(0)

        assert returned_generation is generation
        assert len(records) == 1
        terminal = records[0]
        assert terminal.generation is generation
        assert terminal.protocol_terminated is True
        assert terminal.stdout_eof is True
        assert terminal.process_exited is True
        assert terminal.returncode == 0

    def test_terminal_text_sanitizer_redacts_private_values_and_controls(self):
        """Public diagnostics must not preserve secrets, paths, or control bytes."""

        unsafe = (
            "token=abc123 CLIENT_SECRET=supersecret DB_PASSWORD=hunter2 "
            "C:\\Users\\private\\debug.log /root line\n" + chr(27) + "[31m" + chr(0x9B) + "31m"
        )

        assert sanitize_terminal_text(unsafe) == (
            "token=<redacted> CLIENT_SECRET=<redacted> DB_PASSWORD=<redacted> "
            "<path> <path> line\\n\\u001b[31m\\u009b31m"
        )

    def test_terminal_text_sanitizer_redacts_spaced_authorization_and_quoted_credentials(self):
        """Terminal diagnostics must not leak complete spaced credential values."""
        unsafe = (
            "Authorization: Bearer header token with spaces; "
            'Authorization: "Bearer quoted header token"; '
            'api_key="quoted API key token"; '
            "password='quoted password token';"
        )

        assert sanitize_terminal_text(unsafe) == (
            "Authorization: <redacted>; Authorization: <redacted>; "
            "api_key=<redacted>; password=<redacted>;"
        )

    def test_terminal_event_metadata_is_bounded_before_retention(self):
        """Oversized or invalid DAP event metadata must not persist in state."""

        client = DAPClient("/path")
        client._process = SimpleNamespace(pid=41006, stdout=MagicMock(), returncode=0)
        run = client._run_for_direct_reader()

        client._handle_message(
            {
                "seq": "s" * 10_000,
                "type": "event",
                "event": "e" * 10_000,
                "body": {
                    "token": "abc123",
                    "path": "C:\\Users\\private\\x",
                    "root": "/root",
                    "output": '{"client_secret":"s3cr3t"}',
                    "escaped": r"{\"client_secret\":\"escaped-secret\"}",
                },
            },
            run,
        )

        assert run.last_dap_event is not None
        assert run.last_dap_event[0] is None
        assert len(run.last_dap_event[1]) <= 256
        assert run.last_dap_event_body_preview is not None
        assert "abc123" not in run.last_dap_event_body_preview
        assert "C:\\Users" not in run.last_dap_event_body_preview
        assert "/root" not in run.last_dap_event_body_preview
        assert "s3cr3t" not in run.last_dap_event_body_preview
        assert "escaped-secret" not in run.last_dap_event_body_preview

        client._handle_message(
            {"seq": 10**1000, "type": "event", "event": "bounded", "body": {}},
            run,
        )
        assert run.last_dap_event == (None, "bounded")

    @pytest.mark.asyncio
    async def test_send_pipe_failure_uses_terminal_request_error(self):
        """A send racing finalization must not leak a raw pipe exception."""

        client = DAPClient("/path")
        client._process = SimpleNamespace(
            pid=41007,
            stdin=MagicMock(),
            stdout=MagicMock(),
            returncode=None,
        )
        run = client._run_for_direct_reader()

        async def fail_after_terminalization(_request: DAPRequest) -> None:
            client._settle_pending(run.pending)
            raise BrokenPipeError("adapter stdin closed")

        client._send = fail_after_terminalization

        with pytest.raises(RuntimeError, match="netcoredbg process died"):
            await client.send_request("threads")


class TestOwnerScopedAdapterRedMatrix:
    """Behavior-first RED coverage for the current DAP adapter lifecycle.

    The existing ``_DapRun`` generation/finalizer remains the exercised path.
    These tests require it to obtain an admitted tree owner before it exposes a
    child or publishes terminal state; a process ID is only diagnostic data.
    """

    @pytest.fixture(autouse=True)
    def _use_windows_owner_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("netcoredbg_mcp.dap.client.os.name", "nt")

    @pytest.mark.asyncio
    async def test_o1_adapter_does_not_execute_before_private_owner_admission(self) -> None:
        """O1: the direct adapter route must not run before admission completes.

        The probe records real current launch semantics: awaiting
        ``asyncio.create_subprocess_exec`` means a child is already executing.
        Future production wiring will drive this same seam through suspended
        creation, retained-handle admission, I/O setup, and resume-last.
        """

        events: list[str] = []
        process = TreeProcess(
            pid=43001,
            stdout=BlockingStream(),
            stderr=BlockingStream(),
        )
        client = DAPClient("/path/to/netcoredbg")

        async def launch(**_kwargs: Any) -> OwnedTestProcess:
            events.append("admitted")
            return OwnedTestProcess(process, "o1")

        with patch("netcoredbg_mcp.dap.client.WindowsOwnedProcess.launch", launch):
            await client.start(generation="o1")
        await client.stop()

        assert events == ["admitted"], "adapter launch bypassed private owner admission"

    @pytest.mark.asyncio
    async def test_o5_graceful_adapter_finalization_waits_for_tree_accounting(self) -> None:
        """O5: graceful adapter stop may publish only after the owned tree drains.

        The root exits gracefully while a modeled descendant remains alive.
        Closing/observing the root is not ownership evidence; the terminal
        callback must wait for a retained Job's ``ActiveProcesses == 0`` fact.
        """

        process = TreeProcess(
            pid=43005,
            stdout=BlockingStream(),
            stderr=BlockingStream(),
        )
        active_counts_at_terminal: list[int] = []
        client = DAPClient("/path/to/netcoredbg")
        client.set_transport_terminal_handler(
            lambda _terminal: active_counts_at_terminal.append(process.active_processes)
        )

        with patch(
            "netcoredbg_mcp.dap.client.WindowsOwnedProcess.launch",
            return_value=OwnedTestProcess(process, "o5"),
        ):
            await client.start(generation="o5")
        await client.stop()

        assert active_counts_at_terminal == [0], (
            "current adapter finalizer publishes after root termination without tree accounting"
        )

    @pytest.mark.asyncio
    async def test_o6_grace_deadline_force_drains_only_the_retained_tree(self) -> None:
        """O6: grace expiry must force the admitted Job and wait for its drain.

        This models a root that ignores graceful termination.  A force against
        only that root still leaves its descendant alive, proving why an image,
        PID, or selector replacement cannot meet the owner-only requirement.
        """

        process = TreeProcess(
            pid=43006,
            stdout=BlockingStream(),
            stderr=BlockingStream(),
            root_exits_on_terminate=False,
        )
        active_counts_at_terminal: list[int] = []
        client = DAPClient("/path/to/netcoredbg")
        client.set_transport_terminal_handler(
            lambda _terminal: active_counts_at_terminal.append(process.active_processes)
        )

        with (
            patch(
                "netcoredbg_mcp.dap.client.WindowsOwnedProcess.launch",
                return_value=OwnedTestProcess(process, "o6"),
            ),
            patch("netcoredbg_mcp.dap.client.NATURAL_EXIT_TIMEOUT", 0.001),
            patch("netcoredbg_mcp.dap.client.TERMINATE_TIMEOUT", 0.001),
            patch("netcoredbg_mcp.dap.client.KILL_TIMEOUT", 0.1),
        ):
            await client.start(generation="o6")
            await client.stop()

        assert active_counts_at_terminal == [0], (
            "current grace escalation kills the root but publishes with a live descendant"
        )

    @pytest.mark.asyncio
    async def test_forced_descendant_drain_preserves_natural_root_outcome(self) -> None:
        """Forced Job cleanup of a descendant must not relabel an exited root."""

        class NaturalRootForcedDescendantOwner(OwnedTestProcess):
            async def drain_after_grace(
                self,
                *,
                grace_timeout: float,
                force_timeout: float,
            ) -> OwnerDrainReceipt:
                del grace_timeout, force_timeout
                self._process.returncode = 0
                self._process._root_exit.set()
                self._process.child_alive = False
                return OwnerDrainReceipt(
                    owner=self.owner,
                    status=DrainStatus.DRAINED,
                    forced=True,
                    root_returncode=0,
                    active_processes=0,
                    root_was_forced=False,
                )

        process = TreeProcess(
            pid=43011,
            stdout=BlockingStream(),
            stderr=BlockingStream(),
        )
        owner = NaturalRootForcedDescendantOwner(process, "natural-root")
        client = DAPClient("/path/to/netcoredbg")

        with patch(
            "netcoredbg_mcp.dap.client.WindowsOwnedProcess.launch",
            return_value=owner,
        ):
            await client.start(generation="natural-root")
            receipt = await client.stop(expected_owner=owner.owner)

        run = client._run
        assert receipt is not None
        assert receipt.root_was_forced is False
        assert run is not None and run.terminal is not None
        assert run.terminal.cleanup_outcome is DapCleanupOutcome.NATURAL_EXIT

    @pytest.mark.asyncio
    async def test_expected_owner_mismatch_is_stale_and_match_joins_finalizer(self) -> None:
        """Expected-owner drain refuses stale input and reuses the elected finalizer."""
        process = TreeProcess(
            pid=43010,
            stdout=BlockingStream(),
            stderr=BlockingStream(),
        )
        owner = OwnedTestProcess(process, "generation-a")
        foreign = OwnedProcessRef("foreign", "generation-b", 43011)
        client = DAPClient("/path/to/netcoredbg")

        with (
            patch(
                "netcoredbg_mcp.dap.client.WindowsOwnedProcess.launch",
                return_value=owner,
            ),
            patch("netcoredbg_mcp.dap.client.NATURAL_EXIT_TIMEOUT", 0.001),
            patch("netcoredbg_mcp.dap.client.TERMINATE_TIMEOUT", 0.001),
            patch("netcoredbg_mcp.dap.client.KILL_TIMEOUT", 0.1),
        ):
            await client.start(generation="generation-a")
            stale = await client.stop(expected_owner=foreign)
            receipt = await client.stop(expected_owner=owner.owner)

        assert stale is not None and stale.status is DrainStatus.STALE
        assert process.child_alive is False
        assert receipt is not None
        assert receipt.status is DrainStatus.DRAINED
        assert receipt.owner == owner.owner

    @pytest.mark.asyncio
    async def test_ordinary_stop_returns_final_close_retry_receipt(self) -> None:
        """Ordinary stop returns the final owner receipt after close retries."""

        class RetryingCloseOwner(OwnedTestProcess):
            async def drain_after_grace(
                self,
                *,
                grace_timeout: float,
                force_timeout: float,
            ) -> OwnerDrainReceipt:
                del grace_timeout, force_timeout
                return OwnerDrainReceipt(
                    owner=self.owner,
                    status=DrainStatus.TIMED_OUT,
                    forced=True,
                    root_returncode=None,
                    active_processes=1,
                )

            async def aclose(self) -> OwnerDrainReceipt:
                self._process.kill()
                self._process.child_alive = False
                return OwnerDrainReceipt(
                    owner=self.owner,
                    status=DrainStatus.DRAINED,
                    forced=True,
                    root_returncode=self._process.returncode,
                    active_processes=0,
                )

        process = TreeProcess(
            pid=43012,
            stdout=BlockingStream(),
            stderr=BlockingStream(),
        )
        owner = RetryingCloseOwner(process, "close-retry")
        client = DAPClient("/path/to/netcoredbg")

        with patch(
            "netcoredbg_mcp.dap.client.WindowsOwnedProcess.launch",
            return_value=owner,
        ):
            await client.start(generation="close-retry")
            receipt = await client.stop()

        assert receipt is not None
        assert receipt.status is DrainStatus.DRAINED
        assert receipt.active_processes == 0
