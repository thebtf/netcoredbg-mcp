"""Run-probe runtime-smoke facade tests."""

from __future__ import annotations

import asyncio
import os
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState
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
async def test_runtime_smoke_run_probe_starts_app_diagnostics_probe_run(
    capturing_mcp,
) -> None:
    session = RunProbeFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "app_diagnostics",
            "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
            "app": {"name": "WpfSmokeApp", "process_name": "dotnet"},
            "status": "BLOCKED",
            "observations": [
                {
                    "kind": "ui.backend",
                    "status": "BLOCKED",
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
        },
        name="app-diagnostics-probe",
    )
    data = response["data"]

    assert data["status"] == "RUNNING"
    assert data["run_created"] is True
    assert data["probe"]["kind"] == "app_diagnostics"
    assert data["generated_plan"]["probe_kind"] == "app_diagnostics"
    assert data["validation"]["can_run"] is True

    bundle = await _wait_for_bundle(capturing_mcp, data["run_id"])
    assert bundle["status"] == "BLOCKED"
    assert bundle["result"]["status"] == "BLOCKED"
    assert "runtime_smoke_evidence_bundle" in bundle["next_actions"]
