"""DAP Protocol message types and serialization."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DAPRequest:
    """DAP request message."""
    seq: int
    command: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "seq": self.seq,
            "type": "request",
            "command": self.command,
        }
        if self.arguments:
            d["arguments"] = self.arguments
        return d

    def to_bytes(self) -> bytes:
        content = json.dumps(self.to_dict(), separators=(",", ":"))
        header = f"Content-Length: {len(content)}\r\n\r\n"
        return (header + content).encode("utf-8")


@dataclass
class DAPResponse:
    """DAP response message."""
    seq: int
    request_seq: int
    success: bool
    command: str
    message: str | None = None
    body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DAPResponse:
        return cls(
            seq=data["seq"],
            request_seq=data["request_seq"],
            success=data["success"],
            command=data["command"],
            message=data.get("message"),
            body=data.get("body", {}),
        )


@dataclass
class DAPEvent:
    """DAP event message."""
    seq: int
    event: str
    body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DAPEvent:
        return cls(
            seq=data["seq"],
            event=data["event"],
            body=data.get("body", {}),
        )


def parse_message(data: dict[str, Any]) -> DAPResponse | DAPEvent:
    """Parse a DAP message from dict."""
    msg_type = data.get("type")
    if msg_type == "response":
        return DAPResponse.from_dict(data)
    elif msg_type == "event":
        return DAPEvent.from_dict(data)
    else:
        raise ValueError(f"Unknown message type: {msg_type}")


# Common DAP commands
class Commands:
    INITIALIZE = "initialize"
    LAUNCH = "launch"
    ATTACH = "attach"
    DISCONNECT = "disconnect"
    TERMINATE = "terminate"
    SET_BREAKPOINTS = "setBreakpoints"
    SET_FUNCTION_BREAKPOINTS = "setFunctionBreakpoints"
    SET_EXCEPTION_BREAKPOINTS = "setExceptionBreakpoints"
    CONFIGURATION_DONE = "configurationDone"
    CONTINUE = "continue"
    NEXT = "next"  # step over
    STEP_IN = "stepIn"
    STEP_OUT = "stepOut"
    PAUSE = "pause"
    THREADS = "threads"
    STACK_TRACE = "stackTrace"
    SCOPES = "scopes"
    VARIABLES = "variables"
    EVALUATE = "evaluate"
    EXCEPTION_INFO = "exceptionInfo"


# Common DAP events
class Events:
    INITIALIZED = "initialized"
    STOPPED = "stopped"
    CONTINUED = "continued"
    EXITED = "exited"
    TERMINATED = "terminated"
    THREAD = "thread"
    OUTPUT = "output"
    BREAKPOINT = "breakpoint"
    MODULE = "module"
    PROCESS = "process"
