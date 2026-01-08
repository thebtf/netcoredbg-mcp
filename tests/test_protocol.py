"""Tests for DAP protocol message types."""

import json

import pytest

from netcoredbg_mcp.dap.protocol import (
    Commands,
    DAPEvent,
    DAPRequest,
    DAPResponse,
    Events,
    parse_message,
)


class TestDAPRequest:
    """Tests for DAPRequest dataclass."""

    def test_create_simple_request(self):
        """Test creating a simple request without arguments."""
        req = DAPRequest(seq=1, command="initialize")
        assert req.seq == 1
        assert req.command == "initialize"
        assert req.arguments == {}

    def test_create_request_with_arguments(self):
        """Test creating a request with arguments."""
        req = DAPRequest(
            seq=2,
            command="setBreakpoints",
            arguments={"source": {"path": "test.cs"}, "breakpoints": [{"line": 10}]},
        )
        assert req.seq == 2
        assert req.command == "setBreakpoints"
        assert req.arguments["source"]["path"] == "test.cs"

    def test_to_dict_without_arguments(self):
        """Test converting request to dict without arguments."""
        req = DAPRequest(seq=1, command="threads")
        d = req.to_dict()

        assert d["seq"] == 1
        assert d["type"] == "request"
        assert d["command"] == "threads"
        assert "arguments" not in d

    def test_to_dict_with_arguments(self):
        """Test converting request to dict with arguments."""
        req = DAPRequest(seq=1, command="continue", arguments={"threadId": 1})
        d = req.to_dict()

        assert d["seq"] == 1
        assert d["type"] == "request"
        assert d["command"] == "continue"
        assert d["arguments"]["threadId"] == 1

    def test_to_bytes(self):
        """Test serializing request to bytes with Content-Length header."""
        req = DAPRequest(seq=1, command="threads")
        data = req.to_bytes()

        # Should have Content-Length header
        assert data.startswith(b"Content-Length: ")
        assert b"\r\n\r\n" in data

        # Parse header and content
        header, content = data.split(b"\r\n\r\n", 1)
        content_length = int(header.decode().split(": ")[1])
        assert len(content) == content_length

        # Verify JSON content
        parsed = json.loads(content)
        assert parsed["seq"] == 1
        assert parsed["type"] == "request"
        assert parsed["command"] == "threads"

    def test_to_bytes_compact_json(self):
        """Test that JSON is compact (no extra spaces)."""
        req = DAPRequest(seq=1, command="test", arguments={"key": "value"})
        data = req.to_bytes()
        content = data.split(b"\r\n\r\n")[1]

        # Should not have spaces after colons or commas
        assert b": " not in content
        assert b", " not in content


class TestDAPResponse:
    """Tests for DAPResponse dataclass."""

    def test_from_dict_success(self, sample_dap_response):
        """Test parsing successful response."""
        resp = DAPResponse.from_dict(sample_dap_response)

        assert resp.seq == 1
        assert resp.request_seq == 1
        assert resp.success is True
        assert resp.command == "initialize"
        assert resp.message is None
        assert "supportsConfigurationDoneRequest" in resp.body

    def test_from_dict_failure(self):
        """Test parsing failed response."""
        data = {
            "seq": 5,
            "type": "response",
            "request_seq": 4,
            "success": False,
            "command": "evaluate",
            "message": "Expression evaluation failed",
            "body": {},
        }
        resp = DAPResponse.from_dict(data)

        assert resp.success is False
        assert resp.message == "Expression evaluation failed"

    def test_from_dict_without_body(self):
        """Test parsing response without body."""
        data = {
            "seq": 1,
            "type": "response",
            "request_seq": 1,
            "success": True,
            "command": "disconnect",
        }
        resp = DAPResponse.from_dict(data)

        assert resp.body == {}


class TestDAPEvent:
    """Tests for DAPEvent dataclass."""

    def test_from_dict_stopped_event(self, sample_dap_event):
        """Test parsing stopped event."""
        event = DAPEvent.from_dict(sample_dap_event)

        assert event.seq == 2
        assert event.event == "stopped"
        assert event.body["reason"] == "breakpoint"
        assert event.body["threadId"] == 1

    def test_from_dict_without_body(self):
        """Test parsing event without body."""
        data = {
            "seq": 1,
            "type": "event",
            "event": "initialized",
        }
        event = DAPEvent.from_dict(data)

        assert event.event == "initialized"
        assert event.body == {}

    def test_from_dict_output_event(self):
        """Test parsing output event."""
        data = {
            "seq": 10,
            "type": "event",
            "event": "output",
            "body": {
                "category": "stdout",
                "output": "Hello, World!\n",
            },
        }
        event = DAPEvent.from_dict(data)

        assert event.event == "output"
        assert event.body["category"] == "stdout"
        assert event.body["output"] == "Hello, World!\n"


class TestParseMessage:
    """Tests for parse_message function."""

    def test_parse_response(self, sample_dap_response):
        """Test parsing response message."""
        msg = parse_message(sample_dap_response)

        assert isinstance(msg, DAPResponse)
        assert msg.command == "initialize"

    def test_parse_event(self, sample_dap_event):
        """Test parsing event message."""
        msg = parse_message(sample_dap_event)

        assert isinstance(msg, DAPEvent)
        assert msg.event == "stopped"

    def test_parse_unknown_type(self):
        """Test parsing unknown message type raises error."""
        data = {"seq": 1, "type": "unknown"}

        with pytest.raises(ValueError, match="Unknown message type"):
            parse_message(data)

    def test_parse_missing_type(self):
        """Test parsing message without type raises error."""
        data = {"seq": 1}

        with pytest.raises(ValueError, match="Unknown message type"):
            parse_message(data)


class TestCommandsAndEvents:
    """Tests for command and event constants."""

    def test_commands_values(self):
        """Test command constants have correct values."""
        assert Commands.INITIALIZE == "initialize"
        assert Commands.LAUNCH == "launch"
        assert Commands.SET_BREAKPOINTS == "setBreakpoints"
        assert Commands.CONTINUE == "continue"
        assert Commands.STEP_IN == "stepIn"
        assert Commands.STEP_OUT == "stepOut"
        assert Commands.NEXT == "next"
        assert Commands.EVALUATE == "evaluate"

    def test_events_values(self):
        """Test event constants have correct values."""
        assert Events.INITIALIZED == "initialized"
        assert Events.STOPPED == "stopped"
        assert Events.CONTINUED == "continued"
        assert Events.TERMINATED == "terminated"
        assert Events.OUTPUT == "output"
        assert Events.BREAKPOINT == "breakpoint"
