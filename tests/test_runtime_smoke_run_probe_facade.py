"""Run-probe runtime-smoke facade tests."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
from netcoredbg_mcp.session.state import Breakpoint, BreakpointRegistry, DebugState, TraceEntry
from netcoredbg_mcp.session.tracepoints import TracepointManager
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class RunProbeFacadeSession:
    def __init__(self) -> None:
        self.launch_calls = 0
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.RUNNING,
            process_id=42,
            process_name="RunProbeFacade",
            output_buffer=deque(["ready: alpha\n"]),
            output_sequence=1,
            output_trimmed_before=0,
            modules=[],
            loaded_sources={},
        )
        self.process_registry = None

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        raise AssertionError("run-probe facade must not launch directly")


class GuardedTracepointRunProbeSession(RunProbeFacadeSession):
    def __init__(self) -> None:
        super().__init__()
        self.breakpoints = BreakpointRegistry()
        self._tracepoint_manager = TracepointManager()
        self.hygiene_preflight_calls: list[dict[str, Any]] = []
        self.add_breakpoint_calls: list[tuple[str, int]] = []

    async def add_breakpoint(self, file: str, line: int) -> Breakpoint:
        self.add_breakpoint_calls.append((file, line))
        new_breakpoint = Breakpoint(file=file, line=line, verified=True)
        self.breakpoints.add(new_breakpoint)
        return new_breakpoint

    async def remove_breakpoint(self, file: str, line: int) -> bool:
        return self.breakpoints.remove(file, line)

    async def clear_breakpoints(self, file: str | None = None) -> int:
        return self.breakpoints.clear(file)

    async def configure_exception_breakpoints(self, filters: list[str]) -> bool:
        return filters == []

    def record_trace_hit(self, tracepoint_id: str) -> None:
        tracepoint = self._tracepoint_manager.tracepoints[tracepoint_id]
        self._tracepoint_manager._trace_buffer.append(
            TraceEntry(
                timestamp=time.monotonic(),
                file=tracepoint.file,
                line=tracepoint.line,
                expression=tracepoint.expression,
                value="guarded-route",
                thread_id=1,
                tracepoint_id=tracepoint_id,
            )
        )


class FailingBreakpointTracepointRunProbeSession(GuardedTracepointRunProbeSession):
    async def add_breakpoint(self, file: str, line: int) -> Breakpoint:
        raise RuntimeError(f"adapter refused breakpoint at {file}:{line}")


async def _resolve_project_root(_ctx: Any, _session: Any) -> None:
    raise AssertionError("run-probe facade test must not resolve project paths")


def _register(capturing_mcp, session: RunProbeFacadeSession) -> list[Any]:
    access_calls: list[Any] = []

    def check_access(ctx: Any) -> None:
        access_calls.append(ctx)
        return None

    register_runtime_smoke_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=check_access,
        resolve_project_root=_resolve_project_root,
    )
    return access_calls


async def _wait_for_bundle(capturing_mcp, run_id: str) -> dict[str, Any]:
    for _ in range(20):
        response = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
            ctx=None,
            run_id=run_id,
        )
        data = response["data"]
        if data.get("final"):
            return data
        await asyncio.sleep(0)
    raise AssertionError("runtime smoke run did not finish")


def _app_diagnostics_probe(*, status: str = "BLOCKED") -> dict[str, Any]:
    return {
        "kind": "app_diagnostics",
        "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
        "app": {"name": "WpfSmokeApp", "process_name": "dotnet"},
        "status": status,
        "observations": [
            {
                "kind": "ui.backend",
                "status": status,
                "reason": "GridPattern unavailable",
                "requested": {"control_type": "DataGrid"},
                "accepted": {"fallback": "bounded descendant text"},
                "next_step": "Run WPF fixture replay on a GUI worker.",
            }
        ],
        "redaction": {"omit_fields": ["raw_tree", "screenshot_base64", "secret"]},
        "limits": {
            "max_text_length": 240,
            "max_list_items": 8,
            "max_json_bytes": 32768,
        },
    }


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_rejects_unknown_probe_without_starting_run(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    access_calls = _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={"kind": "ui.colorscheme", "name": "theme"},
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["run_created"] is False
    assert "ui.colorscheme" in "\n".join(data["validation_errors"])
    assert "ui.text" in data["accepted_probe_kinds"]
    assert "runtime_smoke_validate_plan" in response["next_actions"]
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == []
    assert session.runtime_smoke.lifecycle_runs.retained_run_ids() == []
    assert session.launch_calls == 0
    assert len(access_calls) == 1


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_agent_mode_invalid_probe_fails_closed(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={"kind": "ui.colorscheme", "name": "theme"},
        agent_mode=True,
    )
    data = response["data"]
    agent = data["agent_mode"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["run_created"] is False
    assert agent["primary_next_action"] == "runtime_smoke_run_plan"
    assert "next_request" not in agent
    assert "cursor" not in agent
    assert session.runtime_smoke.lifecycle_runs.retained_run_ids() == []


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_starts_durable_probe_run(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    access_calls = _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "process.metric",
            "name": "process_memory",
            "pid": os.getpid(),
        },
        name="ready-probe",
        phase="both",
    )
    data = response["data"]

    assert data["status"] == "RUNNING"
    assert data["run_id"]
    assert data["plan_name"] == "ready-probe"
    assert data["probe"]["kind"] == "process.metric"
    assert data["validation"]["can_run"] is True
    assert data["generated_plan"]["schema"] == "netcoredbg.runtime_smoke.v2"
    assert data["generated_plan"]["case_count"] == 1
    assert data["generated_plan"]["transition_count"] == 1
    assert data["generated_plan"]["action_kind"] == "ui.noop"
    assert data["generated_plan"]["probe_phase"] == "both"
    assert "runtime_smoke_evidence_bundle" in response["next_actions"]
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == [data["run_id"]]
    assert session.launch_calls == 0
    assert len(access_calls) == 1


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_agent_mode_adds_evidence_guidance(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "process.metric",
            "name": "process_memory",
            "pid": os.getpid(),
        },
        name="agent-probe",
        agent_mode=True,
    )
    data = response["data"]

    assert data["agent_mode"]["primary_next_action"] == "runtime_smoke_evidence_bundle"
    assert data["agent_mode"]["next_request"] == {
        "tool": "runtime_smoke_evidence_bundle",
        "arguments": {"run_id": data["run_id"], "agent_mode": True},
    }
    assert data["agent_mode"]["event_cursor_tools"] == [
        "runtime_smoke_mark_event_cursor",
        "runtime_smoke_get_event_delta",
    ]


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_contamination_points_at_cleanup_contract(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    _register(capturing_mcp, session)
    session.runtime_smoke.lifecycle_runs.mark_contaminated(
        reason="runtime smoke cleanup contract required",
        run_id="previous-run",
    )

    response = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "process.metric",
            "name": "process_memory",
            "pid": os.getpid(),
        },
        name="blocked-probe",
        agent_mode=True,
    )
    data = response["data"]

    assert data["status"] == "BLOCKED"
    assert data["run_created"] is False
    assert data["contaminated"] is True
    assert data["agent_mode"]["primary_next_action"] == "runtime_smoke_cleanup_contract"
    assert data["agent_mode"]["next_request"] == {
        "tool": "runtime_smoke_cleanup_contract",
        "arguments": {},
    }
    assert "runtime_smoke_cleanup_contract" in response["next_actions"]
    assert "runtime_smoke_evidence_bundle" not in response["next_actions"]
    assert "runtime_smoke_wait_for_result" not in response["next_actions"]


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_result_is_readable_as_evidence_bundle(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    _register(capturing_mcp, session)

    started = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "process.metric",
            "name": "process_memory",
            "pid": os.getpid(),
        },
        name="ready-bundle",
        phase="both",
    )
    run_id = started["data"]["run_id"]
    data = await _wait_for_bundle(capturing_mcp, run_id)

    assert data["status"] == "PASS"
    assert data["run_id"] == run_id
    assert data["final"] is True
    assert data["result"]["status"] == "PASS"
    assert data["result"]["action_count"] == 1
    assert data["cleanup"]["status"] == "PASS"
    assert [event["kind"] for event in data["events"]] == ["started", "completed"]
    assert "runtime_smoke_evidence_bundle" in data["next_actions"]
    assert "runtime_smoke_run_plan" in data["next_actions"]


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_debug_tracepoint_guard_preflights_and_cleans(
    capturing_mcp,
) -> None:
    session = GuardedTracepointRunProbeSession()
    _register(capturing_mcp, session)
    source = "C:/repo/SettingsViewModel.cs"
    session.breakpoints.add(Breakpoint(file=source, line=42, verified=True))
    stale_tracepoint = session._tracepoint_manager.add(source, 42, "stale")
    stale_tracepoint.breakpoint_id = 9001
    session._tracepoint_manager._trace_buffer.append(
        TraceEntry(
            timestamp=time.monotonic(),
            file=source,
            line=42,
            expression="stale",
            value="old",
            thread_id=1,
            tracepoint_id="stale-tp",
        )
    )

    started = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "debug.tracepoint",
            "name": "settings_route_guard",
            "file": source,
            "line": 42,
            "expression": "Mode.SpellCheckInput",
            "expected_hit_count": 0,
        },
        name="debug-tracepoint-guard",
        phase="both",
        debug_preflight=True,
        tracepoint_guard={
            "cleanup": {
                "owner": "runtime_smoke_run_probe",
                "operations": ["debug.tracepoint.remove", "debug.trace_log.clear"],
            }
        },
        agent_mode=True,
    )
    data = started["data"]

    assert data["status"] == "RUNNING"
    assert data["run_created"] is True
    assert data["probe"]["kind"] == "debug.tracepoint"
    assert data["generated_plan"]["debug_preflight"] is True
    assert data["generated_plan"]["tracepoint_guard"]["cleanup_operations"] == [
        "debug.tracepoint.remove",
        "debug.trace_log.clear",
    ]
    assert data["agent_mode"]["primary_next_action"] == "runtime_smoke_evidence_bundle"

    active_blocked = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "debug.tracepoint",
            "name": "second_guard",
            "file": source,
            "line": 42,
            "expression": "Mode.SpellCheckInput",
        },
        name="debug-tracepoint-overlap",
        phase="both",
        debug_preflight=True,
        agent_mode=True,
    )
    active_data = active_blocked["data"]

    assert active_data["status"] == "BLOCKED"
    assert active_data["active_run_id"] == data["run_id"]
    assert active_data["agent_mode"]["primary_next_action"] == "runtime_smoke_evidence_bundle"
    assert active_data["agent_mode"]["next_request"]["arguments"]["run_id"] == data["run_id"]

    bundle = await _wait_for_bundle(capturing_mcp, data["run_id"])

    assert bundle["status"] == "PASS"
    assert bundle["final"] is True
    assert bundle["result"]["status"] == "PASS"
    assert "baseline" not in bundle["result"]
    preflight_step = bundle["result"]["debug_preflight"]["steps"][0]
    assert preflight_step["kind"] == "debug_hygiene_preflight"
    assert preflight_step["cleared"] == {
        "breakpoints": 1,
        "trace_log_entries": 1,
        "exception_filters": 0,
    }
    assert preflight_step["tracepoints_removed"] == 1
    assert session.add_breakpoint_calls == [(source, 42)]
    assert bundle["cleanup"]["status"] == "PASS"
    assert bundle["cleanup"]["tracepoints_removed"] == 1
    assert bundle["cleanup"]["attempted"] == [
        "case:run_probe:debug.tracepoint.remove:SettingsViewModel.cs:42",
        "case:run_probe:debug.trace_log.clear",
    ]
    delta = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={"run_id": data["run_id"], "after_cursor": 0},
        event_limit=10,
    )
    delta_data = delta["data"]

    assert delta_data["status"] == "PASS"
    assert delta_data["run_id"] == data["run_id"]
    assert [event["kind"] for event in delta_data["events"]] == ["started", "completed"]
    assert delta_data["final"] is True
    assert session.breakpoints.get_for_file(source) == []
    assert session._tracepoint_manager.tracepoints == {}
    assert session._tracepoint_manager.get_log() == []


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_debug_tracepoint_rolls_back_failed_breakpoint_arm(
    capturing_mcp,
) -> None:
    session = FailingBreakpointTracepointRunProbeSession()
    _register(capturing_mcp, session)
    source = "C:/repo/SettingsViewModel.cs"

    started = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "debug.tracepoint",
            "name": "settings_route_guard",
            "file": source,
            "line": 42,
            "expression": "Mode.SpellCheckInput",
            "expected_hit_count": 0,
        },
        name="debug-tracepoint-arm-failure",
        phase="both",
        debug_preflight=True,
        agent_mode=True,
    )
    data = started["data"]

    assert data["status"] == "RUNNING"
    bundle = await _wait_for_bundle(capturing_mcp, data["run_id"])

    assert bundle["status"] == "BLOCKED"
    assert bundle["result"]["status"] == "BLOCKED"
    assert "breakpoint arming failed" in bundle["result"]["reason"]
    assert session._tracepoint_manager.tracepoints == {}


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_debug_tracepoint_ignores_unrelated_breakpoint(
    capturing_mcp,
) -> None:
    session = GuardedTracepointRunProbeSession()
    _register(capturing_mcp, session)
    source = "C:/repo/SettingsViewModel.cs"
    session.breakpoints.add(Breakpoint(file=source, line=99, verified=True))

    started = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "debug.tracepoint",
            "name": "settings_route_guard",
            "file": source,
            "line": 42,
            "expression": "Mode.SpellCheckInput",
            "expected_hit_count": 0,
        },
        name="debug-tracepoint-unrelated-breakpoint",
        phase="both",
        tracepoint_guard={
            "cleanup": {
                "owner": "runtime_smoke_run_probe",
                "operations": ["debug.tracepoint.remove"],
            }
        },
        agent_mode=True,
    )
    data = started["data"]

    assert data["status"] == "RUNNING"
    bundle = await _wait_for_bundle(capturing_mcp, data["run_id"])

    assert bundle["status"] == "PASS"
    assert session.add_breakpoint_calls == [(source, 42)]
    assert [bp.line for bp in session.breakpoints.get_for_file(source)] == [99]


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_debug_tracepoint_rejects_unsafe_expression_before_arming(
    capturing_mcp,
) -> None:
    session = GuardedTracepointRunProbeSession()
    _register(capturing_mcp, session)
    source = "C:/repo/SettingsViewModel.cs"

    started = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "debug.tracepoint",
            "name": "settings_route_guard",
            "file": source,
            "line": 42,
            "expression": "Mode.Reset(); Mode.SpellCheckInput",
            "expected_hit_count": 1,
        },
        name="debug-tracepoint-unsafe-expression",
        phase="both",
        debug_preflight=True,
        agent_mode=True,
    )
    data = started["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["run_created"] is False
    assert any("unsafe tracepoint expression" in error for error in data["validation_errors"])
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == []
    assert session.runtime_smoke.lifecycle_runs.retained_run_ids() == []
    assert session.add_breakpoint_calls == []
    assert session.breakpoints.get_for_file(source) == []


@pytest.mark.asyncio
async def test_debug_tracepoint_adapter_rejects_unsafe_expression_before_arming() -> None:
    session = GuardedTracepointRunProbeSession()
    source = "C:/repo/SettingsViewModel.cs"

    async def unused_backend() -> None:
        raise AssertionError("debug.tracepoint must not need the UI backend")

    adapters = ui_operation_adapters(unused_backend, session=session)
    result = await adapters["debug.tracepoint"](
        file=source,
        line=42,
        expression="Mode.Reset(); Mode.SpellCheckInput",
    )

    assert result["status"] == "FAIL"
    assert result["classification"] == "UNSAFE_EXPRESSION"
    assert result["reason"] == "unsafe tracepoint expression"
    assert session.add_breakpoint_calls == []
    assert session._tracepoint_manager.tracepoints == {}
    assert session.breakpoints.get_for_file(source) == []


@pytest.mark.asyncio
async def test_debug_tracepoint_adapter_fails_closed_on_stale_same_location_expression() -> None:
    session = GuardedTracepointRunProbeSession()
    source = "C:/repo/SettingsViewModel.cs"
    session.breakpoints.add(Breakpoint(file=source, line=42, verified=True, id=9001))
    stale_tracepoint = session._tracepoint_manager.add(
        source,
        42,
        "Mode.Reset(); Mode.SpellCheckInput",
    )
    stale_tracepoint.breakpoint_id = 9001

    async def unused_backend() -> None:
        raise AssertionError("debug.tracepoint must not need the UI backend")

    adapters = ui_operation_adapters(unused_backend, session=session)
    result = await adapters["debug.tracepoint"](
        file=source,
        line=42,
        expression="Mode.SpellCheckInput",
    )

    assert result["status"] == "BLOCKED"
    assert result["classification"] == "TRACEPOINT_POLICY_CONFLICT"
    assert result["existing_tracepoint_id"] == stale_tracepoint.id
    assert result["next_step"]
    tracepoints = session._tracepoint_manager.tracepoints
    assert list(tracepoints) == [stale_tracepoint.id]
    assert tracepoints[stale_tracepoint.id].expression == "Mode.Reset(); Mode.SpellCheckInput"
    assert session.add_breakpoint_calls == []


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_rejects_unsafe_debug_tracepoint_expression(
    capturing_mcp,
) -> None:
    session = GuardedTracepointRunProbeSession()
    _register(capturing_mcp, session)
    source = "C:/repo/SettingsViewModel.cs"

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan={
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "unsafe-tracepoint-plan",
            "cases": [
                {
                    "id": "unsafe_tracepoint_case",
                    "transitions": [
                        {
                            "id": "probe",
                            "action": {"kind": "ui.noop"},
                            "probes": [
                                {
                                    "kind": "debug.tracepoint",
                                    "name": "unsafe_tracepoint",
                                    "file": source,
                                    "line": 42,
                                    "expression": "Mode.Reset(); Mode.SpellCheckInput",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert any("unsafe tracepoint expression" in error for error in data["validation_errors"])
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == []
    assert session.add_breakpoint_calls == []


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_starts_oracle_pack_probe_run(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "oracle_pack",
            "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
            "id": "wpf-grid-oracle-pack",
            "status": "PASS",
            "checks": [
                {
                    "id": "visible-row-count",
                    "probe": "ui.grid",
                    "expect": {"min_rows": 1},
                    "on_blocked": {"next_step": "Run WPF fixture replay."},
                }
            ],
            "limits": {
                "max_text_length": 240,
                "max_list_items": 8,
                "max_json_bytes": 32768,
            },
        },
        name="oracle-pack-probe",
    )
    data = response["data"]

    assert data["status"] == "RUNNING"
    assert data["run_created"] is True
    assert data["probe"]["kind"] == "oracle_pack"
    assert data["generated_plan"]["probe_kind"] == "oracle_pack"
    assert data["validation"]["can_run"] is True

    bundle = await _wait_for_bundle(capturing_mcp, data["run_id"])
    assert bundle["status"] == "PASS"
    assert bundle["result"]["status"] == "PASS"
    assert "runtime_smoke_evidence_bundle" in bundle["next_actions"]


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_returns_disagreeing_oracle_pack_bundle(
    capturing_mcp,
    tmp_path,
) -> None:
    session = RunProbeFacadeSession()
    _register(capturing_mcp, session)
    text_source = tmp_path / "text-source.json"
    property_source = tmp_path / "property-source.json"
    text_source.write_text('{"status": "Ready"}', encoding="utf-8")
    property_source.write_text('{"status": "Busy"}', encoding="utf-8")

    started = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "oracle_pack",
            "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
            "id": "status-oracle-pack",
            "status": "PASS",
            "checks": [
                {
                    "id": "status-agrees",
                    "probe": "file.json",
                    "expect": {"value": "Ready"},
                    "on_blocked": {"next_step": "Inspect status evidence."},
                }
            ],
            "sources": [
                {
                    "id": "status_text",
                    "probe": {
                        "kind": "file.json",
                        "path": str(text_source),
                        "jsonpath": "$.status",
                    },
                },
                {
                    "id": "status_property",
                    "probe": {
                        "kind": "file.json",
                        "path": str(property_source),
                        "jsonpath": "$.status",
                    },
                },
            ],
            "limits": {
                "max_text_length": 240,
                "max_list_items": 8,
                "max_json_bytes": 32768,
            },
        },
        name="oracle-pack-disagreeing-sources",
    )
    data = started["data"]

    assert data["status"] == "RUNNING"
    assert data["run_created"] is True
    bundle = await _wait_for_bundle(capturing_mcp, data["run_id"])

    assert bundle["status"] == "BLOCKED"
    assert bundle["result"]["status"] == "BLOCKED"
    assert bundle["result"]["reason"] == "DISAGREEING_SOURCES"
    assert bundle["result"]["evidence_refs"] == [
        {
            "case_id": "run_probe",
            "phase": "after",
            "probe": "oracle_pack",
            "evidence_ref": "diagnostic:oracle_pack:status-oracle-pack",
        }
    ]


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_rejects_invalid_oracle_pack_before_run(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "oracle_pack",
            "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
            "id": "broken-oracle-pack",
            "status": "PASS",
            "checks": [],
        },
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["run_created"] is False
    assert any("oracle_pack.limits is required" in error for error in data["validation_errors"])
    assert "oracle_pack" in data["accepted_probe_kinds"]
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == []
    assert session.runtime_smoke.lifecycle_runs.retained_run_ids() == []


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_advertises_app_diagnostics_launch_contract(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe=_app_diagnostics_probe(),
        name="app-diagnostics-probe",
    )
    data = response["data"]

    diagnostic_launch = data["generated_plan"]["diagnostic_launch"]
    assert diagnostic_launch["kind"] == "app_diagnostics"
    assert diagnostic_launch["schema"] == "netcoredbg.runtime_smoke.diagnostics.v1"
    assert diagnostic_launch["env_var_names"] == {
        "directory": "NETCOREDBG_MCP_APP_DIAGNOSTICS_DIR",
        "path": "NETCOREDBG_MCP_APP_DIAGNOSTICS_PATH",
        "schema": "NETCOREDBG_MCP_APP_DIAGNOSTICS_SCHEMA",
    }
    assert diagnostic_launch["evidence"]["directory"] == (
        ".agent/runtime-smoke/app-diagnostics/app-diagnostics-probe"
    )
    assert diagnostic_launch["evidence"]["path"] == (
        ".agent/runtime-smoke/app-diagnostics/app-diagnostics-probe/app-diagnostics.json"
    )
    assert diagnostic_launch["redacted_env_values"] is True
    assert "env" not in diagnostic_launch
    assert "env_values" not in diagnostic_launch
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_bundle_preserves_app_diagnostics_launch_contract(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    _register(capturing_mcp, session)

    started = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe=_app_diagnostics_probe(),
        name="app-diagnostics-probe",
    )
    run_id = started["data"]["run_id"]
    expected = started["data"]["generated_plan"]["diagnostic_launch"]

    bundle = await _wait_for_bundle(capturing_mcp, run_id)

    assert bundle["diagnostic_launch"] == expected
    assert bundle["result"]["diagnostic_launch"] == expected
    assert bundle["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_runtime_smoke_run_probe_starts_app_diagnostics_probe_run(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe=_app_diagnostics_probe(),
        name="app-diagnostics-probe",
    )
    data = response["data"]

    assert data["status"] == "RUNNING"
    assert data["run_created"] is True
    assert data["probe"]["kind"] == "app_diagnostics"
    assert data["generated_plan"]["probe_kind"] == "app_diagnostics"
    assert data["validation"]["can_run"] is True
    assert "runtime_smoke_wait_for_result" in response["next_actions"]

    bundle = await _wait_for_bundle(capturing_mcp, data["run_id"])
    assert bundle["status"] == "BLOCKED"
    assert bundle["result"]["status"] == "BLOCKED"
    assert "runtime_smoke_wait_for_result" in bundle["next_actions"]
    assert "runtime_smoke_evidence_bundle" in bundle["next_actions"]
