"""Pytest fixtures for netcoredbg-mcp tests."""

from __future__ import annotations

import os
import sys
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture
def mock_netcoredbg_path():
    """Mock netcoredbg lookup for server registration tests."""
    with patch.dict(os.environ, {"NETCOREDBG_PATH": "/fake/netcoredbg"}):
        with patch(
            "netcoredbg_mcp.dap.client.DAPClient._find_netcoredbg",
            return_value="/fake/netcoredbg",
        ):
            yield


@dataclass
class FakeSmokeSession:
    """Minimal shared fake for runtime smoke service tests."""
    breakpoints: dict[str, list[object]] = field(default_factory=dict)
    tracepoints: dict[str, object] = field(default_factory=dict)
    output_buffer: deque[object] = field(default_factory=deque)
    modules: list[object] = field(default_factory=list)
    loaded_sources: dict[str, object] = field(default_factory=dict)
    process_id: int | None = None
    state: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(state="idle")
    )


@pytest.fixture
def fake_smoke_session() -> FakeSmokeSession:
    """Return an isolated fake session for runtime smoke tests."""
    return FakeSmokeSession()


@pytest.fixture
def sample_breakpoint_data():
    """Sample breakpoint data for testing."""
    return {
        "file": "C:/test/Program.cs",
        "line": 10,
        "condition": "x > 5",
        "hit_condition": "3",
    }


@pytest.fixture
def sample_dap_response():
    """Sample DAP response data."""
    return {
        "seq": 1,
        "type": "response",
        "request_seq": 1,
        "success": True,
        "command": "initialize",
        "body": {
            "supportsConfigurationDoneRequest": True,
            "supportsFunctionBreakpoints": True,
        },
    }


@pytest.fixture
def sample_dap_event():
    """Sample DAP event data."""
    return {
        "seq": 2,
        "type": "event",
        "event": "stopped",
        "body": {
            "reason": "breakpoint",
            "threadId": 1,
            "allThreadsStopped": True,
        },
    }


@pytest.fixture
def sample_stack_frames():
    """Sample stack frames data."""
    return [
        {
            "id": 0,
            "name": "Program.Main()",
            "source": {"path": "C:/test/Program.cs"},
            "line": 10,
            "column": 1,
        },
        {
            "id": 1,
            "name": "System.Runtime.Main()",
            "source": None,
            "line": 0,
            "column": 0,
        },
    ]


@pytest.fixture
def sample_variables():
    """Sample variables data."""
    return [
        {
            "name": "x",
            "value": "10",
            "type": "int",
            "variablesReference": 0,
        },
        {
            "name": "args",
            "value": "{string[0]}",
            "type": "string[]",
            "variablesReference": 5,
            "namedVariables": 0,
            "indexedVariables": 0,
        },
    ]
