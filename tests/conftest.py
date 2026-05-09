"""Pytest fixtures for netcoredbg-mcp tests."""

from __future__ import annotations

import os
import sys
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
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


class CapturingMCP:
    """Minimal MCP test double that records decorated tools by function name."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture
def capturing_mcp() -> CapturingMCP:
    """Return an isolated tool-capturing MCP test double."""
    return CapturingMCP()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--critical-only",
        action="store_true",
        default=False,
        help="Run only tests marked with @pytest.mark.critical.",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    if not config.getoption("--critical-only"):
        return
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if item.get_closest_marker("critical") is not None:
            selected.append(item)
        else:
            deselected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected


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
