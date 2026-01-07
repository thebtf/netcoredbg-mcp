"""Pytest fixtures for netcoredbg-mcp tests."""

import pytest
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


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
