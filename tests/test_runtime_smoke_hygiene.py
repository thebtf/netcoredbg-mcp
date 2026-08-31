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
from netcoredbg_mcp.session.tracepoints import TracepointManager
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools
from tests import smoke_test_manual


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


async def _noop_resolve_project_root(ctx: Any, session: Any) -> None:
    pass


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

    skipped = _as_dict(
        await RuntimeHygieneService(session).preflight(
            clear_trace_log=False,
            clear_exception_filters=False,
        )
    )
    assert skipped["cleared"]["trace_log_entries"] == 0
    assert skipped["cleared"]["exception_filters"] == 0
    assert session.exception_calls == []

    applied = _as_dict(
        await RuntimeHygieneService(session).preflight(
            clear_trace_log=True,
            clear_exception_filters=True,
        )
    )
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
async def test_preflight_removes_stale_tracepoint_definitions_for_file_scope() -> None:
    target = "C:/repo/Target.cs"
    unrelated = "C:/repo/Other.cs"
    session = FakeHygieneSession()
    manager = TracepointManager()
    manager.add(target, 10, "stale target")
    unrelated_tracepoint = manager.add(unrelated, 20, "keep")
    session._tracepoint_manager = manager
    session.breakpoints.add(Breakpoint(file=target, line=10))

    result = _as_dict(await RuntimeHygieneService(session).preflight(file=target))

    assert result["status"] == "PASS"
    assert result["cleared"]["breakpoints"] == 1
    assert result["tracepoints_removed"] == 1
    assert manager.find_tracepoint_for_location(target, 10) is None
    assert manager.find_tracepoint_for_location(unrelated, 20).id == unrelated_tracepoint.id


@pytest.mark.asyncio
async def test_preflight_file_scope_does_not_remove_same_filename_tracepoint() -> None:
    target = "C:/repo/src/Program.cs"
    same_name = "C:/repo/tests/Program.cs"
    session = FakeHygieneSession()
    manager = TracepointManager()
    manager.add(target, 10, "target")
    same_name_tracepoint = manager.add(same_name, 20, "keep")
    session._tracepoint_manager = manager
    session.breakpoints.add(Breakpoint(file=target, line=10))

    result = _as_dict(await RuntimeHygieneService(session).preflight(file=target))

    assert result["status"] == "PASS"
    assert result["tracepoints_removed"] == 1
    assert manager.find_tracepoint_for_location(target, 10) is None
    assert manager.tracepoints == {same_name_tracepoint.id: same_name_tracepoint}


@pytest.mark.asyncio
async def test_debug_hygiene_preflight_tool_returns_fail_for_invalid_file_scope(
    capturing_mcp,
) -> None:
    mcp = capturing_mcp
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


def _manual_smoke_list(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "tests/smoke_test_manual.py", "--list", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_manual_smoke_default_inventory_keeps_only_gallery_gui_scenarios() -> None:
    default_names = {name for name, _fn in smoke_test_manual.get_scenarios()}
    full_names = {name for name, _fn in smoke_test_manual.get_scenarios(include_extended_gui=True)}
    extended_examples = {
        "UI Invoke + Toggle + Root ID",
        "WPF V2 State Oracle Runtime Smoke",
        "Avalonia UI Fixture Compatibility",
        "Typed BitBlt Fallback Native Bridge",
    }

    assert {"WPF Smoke Gallery", "WinForms Smoke Gallery"} <= default_names
    assert extended_examples.isdisjoint(default_names)
    assert extended_examples <= full_names
    assert "Runtime Hygiene Preflight" in default_names


def test_manual_smoke_list_respects_extended_gui_flag() -> None:
    default_output = _manual_smoke_list()
    extended_output = _manual_smoke_list("--extended-gui")

    assert "WPF Smoke Gallery" in default_output
    assert "WinForms Smoke Gallery" in default_output
    assert "WPF V2 State Oracle Runtime Smoke" not in default_output
    assert "Avalonia UI Fixture Compatibility" not in default_output
    assert "WPF V2 State Oracle Runtime Smoke" in extended_output
    assert "Avalonia UI Fixture Compatibility" in extended_output
    assert "Startup Temp GC Prefix Filter" in default_output
    assert "Typed BitBlt Fallback Native Bridge" in extended_output


def test_manual_smoke_exact_selection_resolves_extended_inventory() -> None:
    selected = smoke_test_manual._resolve_scenarios({"WPF V2 State Oracle Runtime Smoke"})

    assert selected == [
        (
            "WPF V2 State Oracle Runtime Smoke",
            smoke_test_manual.test_wpf_v2_state_oracle_runtime_smoke,
        )
    ]


class _GallerySession:
    def __init__(self) -> None:
        self.state = SimpleNamespace(process_id=7001)
        self.process_registry = object()
        self.launches: list[dict[str, Any]] = []
        self.stop_calls = 0

    async def launch(self, **kwargs: Any) -> None:
        self.launches.append(kwargs)

    async def stop(self) -> None:
        self.stop_calls += 1


class _GalleryBackend:
    def __init__(self) -> None:
        self.connect_pids: list[int] = []
        self.disconnect_calls = 0

    async def connect(self, pid: int) -> None:
        self.connect_pids.append(pid)

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


@pytest.mark.asyncio
async def test_gui_smoke_gallery_owner_runs_one_lifecycle() -> None:
    session = _GallerySession()
    backend = _GalleryBackend()

    async with smoke_test_manual._GuiSmokeGallery(
        program="fixture.dll",
        cwd="fixture",
        args=["gui"],
        session_factory=lambda: session,
        backend_factory=lambda actual_session: backend,
    ) as gallery:
        assert gallery.session is session
        assert gallery.backend is backend
        assert session.launches == [{"program": "fixture.dll", "cwd": "fixture", "args": ["gui"]}]
        assert backend.connect_pids == [7001]

    assert backend.disconnect_calls == 1
    assert session.stop_calls == 1


@pytest.mark.asyncio
async def test_gui_smoke_gallery_owner_closes_once_after_failure() -> None:
    session = _GallerySession()
    backend = _GalleryBackend()

    with pytest.raises(RuntimeError, match="gallery failure"):
        async with smoke_test_manual._GuiSmokeGallery(
            program="fixture.dll",
            session_factory=lambda: session,
            backend_factory=lambda actual_session: backend,
        ):
            raise RuntimeError("gallery failure")

    assert len(session.launches) == 1
    assert backend.connect_pids == [7001]
    assert backend.disconnect_calls == 1
    assert session.stop_calls == 1


@pytest.mark.asyncio
async def test_gui_smoke_gallery_retries_missing_automation_id_by_name() -> None:
    calls: list[dict[str, str]] = []

    async def find(**selector: str) -> dict[str, Any]:
        calls.append(selector)
        return {"found": "name" in selector, **selector}

    result = await smoke_test_manual._call_by_automation_id_or_name(find, "btnInvoke")

    assert result == {"found": True, "name": "btnInvoke"}
    assert calls == [
        {"automation_id": "btnInvoke"},
        {"name": "btnInvoke"},
    ]
