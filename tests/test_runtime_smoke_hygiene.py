"""Runtime smoke hygiene preflight contract tests."""

from __future__ import annotations

import os
import subprocess
import sys
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.server import create_server
from netcoredbg_mcp.session.hygiene import RuntimeHygieneService
from netcoredbg_mcp.session.state import Breakpoint, BreakpointRegistry, DebugState
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class FakeTracepointManager:
    def __init__(self, entries: int = 0) -> None:
        self._entries = entries

    def clear_log(self) -> int:
        count = self._entries
        self._entries = 0
        return count


class FakeHygieneSession:
    def __init__(
        self,
        *,
        state: DebugState = DebugState.IDLE,
        active: bool = False,
        trace_entries: int = 0,
    ) -> None:
        self.breakpoints = BreakpointRegistry()
        self.state = SimpleNamespace(state=state, output_buffer=deque())
        self.is_active = active
        self.exception_calls: list[list[str]] = []
        self.exception_success = True
        self.clear_failure: str | None = None
        self.validation_failure: str | None = None
        self._tracepoint_manager = FakeTracepointManager(trace_entries)

    async def clear_breakpoints(self, file: str | None = None) -> int:
        if self.clear_failure:
            raise RuntimeError(self.clear_failure)
        return self.breakpoints.clear(file)

    async def configure_exception_breakpoints(self, filters: list[str]) -> bool:
        self.exception_calls.append(list(filters))
        return self.exception_success

    def validate_path(self, file: str, must_exist: bool = True) -> str:
        if self.validation_failure:
            raise ValueError(self.validation_failure)
        return file


class CapturingMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


async def _noop_resolve_project_root(ctx: Any, session: Any) -> None:
    return None


def _as_dict(result: Any) -> dict[str, Any]:
    return result.to_dict()


@pytest.mark.asyncio
async def test_preflight_passes_for_idle_and_stopped_sessions_with_zero_counts() -> None:
    idle = FakeHygieneSession(state=DebugState.IDLE, active=False)
    stopped = FakeHygieneSession(state=DebugState.STOPPED, active=True)

    idle_result = _as_dict(await RuntimeHygieneService(idle).preflight())
    stopped_result = _as_dict(await RuntimeHygieneService(stopped).preflight())

    for result in (idle_result, stopped_result):
        assert result["status"] == "PASS"
        assert result["cleared"] == {
            "breakpoints": 0,
            "trace_log_entries": 0,
            "exception_filters": 0,
        }
        assert result["remaining_breakpoints"] == []
        assert result["cleanup_errors"] == []


@pytest.mark.asyncio
async def test_preflight_fails_with_file_line_evidence_when_targeted_breakpoint_remains() -> None:
    source = "C:/repo/Program.cs"
    session = FakeHygieneSession()
    session.breakpoints.add(Breakpoint(file=source, line=42, verified=True))
    session.clear_failure = "debug adapter rejected breakpoint cleanup"

    result = _as_dict(await RuntimeHygieneService(session).preflight(file=source))

    assert result["status"] == "FAIL"
    assert result["reason"] == "targeted breakpoints remain after hygiene preflight"
    assert result["remaining_breakpoints"] == [
        {
            "file": os.path.normpath(source),
            "line": 42,
            "dap_line": None,
            "condition": None,
            "verified": True,
        }
    ]
    assert result["cleanup_errors"] == [
        {
            "operation": "clear_breakpoints",
            "error": "debug adapter rejected breakpoint cleanup",
        }
    ]


@pytest.mark.asyncio
async def test_trace_log_and_exception_filter_flags_are_independently_applied() -> None:
    session = FakeHygieneSession(active=True, trace_entries=3)

    skipped = _as_dict(await RuntimeHygieneService(session).preflight(
        clear_trace_log=False,
        clear_exception_filters=False,
    ))
    assert skipped["cleared"]["trace_log_entries"] == 0
    assert skipped["cleared"]["exception_filters"] == 0
    assert session.exception_calls == []

    applied = _as_dict(await RuntimeHygieneService(session).preflight(
        clear_trace_log=True,
        clear_exception_filters=True,
    ))
    assert applied["status"] == "PASS"
    assert applied["cleared"]["trace_log_entries"] == 3
    assert applied["cleared"]["exception_filters"] == 1
    assert session.exception_calls == [[]]


@pytest.mark.asyncio
async def test_scoped_file_cleanup_preserves_unrelated_breakpoints() -> None:
    target = "C:/repo/Target.cs"
    unrelated = "C:/repo/Other.cs"
    session = FakeHygieneSession()
    session.breakpoints.add(Breakpoint(file=target, line=10))
    session.breakpoints.add(Breakpoint(file=unrelated, line=20))

    result = _as_dict(await RuntimeHygieneService(session).preflight(file=target))

    assert result["status"] == "PASS"
    assert result["cleared"]["breakpoints"] == 1
    assert result["remaining_breakpoints"] == []
    assert session.breakpoints.get_for_file(target) == []
    assert [bp.line for bp in session.breakpoints.get_for_file(unrelated)] == [20]


@pytest.mark.asyncio
async def test_debug_hygiene_preflight_tool_returns_fail_for_invalid_file_scope() -> None:
    mcp = CapturingMCP()
    session = FakeHygieneSession()
    session.validation_failure = "Path outside project root"

    register_runtime_smoke_tools(
        mcp=mcp,
        session=session,
        check_session_access=lambda ctx: None,
        resolve_project_root=_noop_resolve_project_root,
    )

    response = await mcp.tools["debug_hygiene_preflight"](
        ctx=None,
        file="C:/outside/Program.cs",
    )

    assert response["state"] == "idle"
    assert "debug_hygiene_preflight" in response["next_actions"]
    assert response["data"]["status"] == "FAIL"
    assert response["data"]["reason"] == "invalid file scope"
    assert response["data"]["validation_error"] == "Path outside project root"


@pytest.mark.asyncio
async def test_server_registers_debug_hygiene_preflight(mock_netcoredbg_path) -> None:
    server = create_server(str(os.getcwd()))

    tools = await server.list_tools()
    tool_names = {tool.name for tool in tools}

    assert "debug_hygiene_preflight" in tool_names
    assert "clear_breakpoints" in tool_names
    assert "get_output" in tool_names


def test_manual_smoke_list_includes_hygiene_scenario() -> None:
    result = subprocess.run(
        [sys.executable, "tests/smoke_test_manual.py", "--list"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Runtime Hygiene Preflight" in result.stdout
