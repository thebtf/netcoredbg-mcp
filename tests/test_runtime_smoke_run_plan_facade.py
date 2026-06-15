"""Run-plan and evidence-bundle runtime-smoke facade tests."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class RunPlanFacadeSession:
    def __init__(self) -> None:
        self.launch_calls = 0
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.RUNNING,
            process_id=42,
            process_name="RunPlanFacade",
            output_buffer=deque(),
            output_sequence=0,
            output_trimmed_before=0,
            modules=[],
            loaded_sources={},
        )
        self.process_registry = None

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        raise AssertionError("run-plan facade must not launch directly")


async def _resolve_project_root(_ctx: Any, _session: Any) -> None:
    raise AssertionError("run-plan facade test plan must not resolve project paths")


def _register(capturing_mcp, session: RunPlanFacadeSession) -> list[Any]:
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


async def _wait_for_final_bundle(capturing_mcp, run_id: str) -> dict[str, Any]:
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
async def test_runtime_smoke_run_plan_rejects_invalid_plan_without_starting_run(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    access_calls = _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={"name": "invalid", "actions": "not-a-list"},
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == ["actions must be a list"]
    assert "run_id" not in data
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == []
    assert session.runtime_smoke.lifecycle_runs.retained_run_ids() == []
    assert session.launch_calls == 0
    assert len(access_calls) == 1


@pytest.mark.asyncio
async def test_runtime_smoke_run_plan_starts_durable_run_after_validation(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    access_calls = _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "name": "facade-run",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        },
    )
    data = response["data"]

    assert data["status"] == "RUNNING"
    assert data["run_id"]
    assert data["plan_name"] == "facade-run"
    assert data["final"] is False
    assert data["validation"]["can_run"] is True
    assert data["validation"]["validation_errors"] == []
    assert session.runtime_smoke.lifecycle_runs.active_run_ids() == [data["run_id"]]
    assert session.launch_calls == 0
    assert len(access_calls) == 1


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_bundle_returns_bounded_final_packet(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    started = await capturing_mcp.tools["runtime_smoke_run_plan"](
        ctx=None,
        plan={
            "name": "facade-bundle",
            "actions": [{"name": "output_checkpoint", "args": {"name": "start"}}],
        },
    )
    run_id = started["data"]["run_id"]
    data = await _wait_for_final_bundle(capturing_mcp, run_id)

    assert data["status"] == "PASS"
    assert data["run_id"] == run_id
    assert data["final"] is True
    assert data["result"]["status"] == "PASS"
    assert data["result"]["action_count"] == 1
    assert data["cleanup"]["status"] == "PASS"
    assert data["evidence_refs"] == [
        {
            "kind": "output_checkpoint",
            "ref": "output:start",
            "summary": "output checkpoint created",
        }
    ]
    assert [event["kind"] for event in data["events"]] == ["started", "completed"]
    assert data["event_cursor"]["next_cursor"] >= 2
    assert data["event_cursor"]["oldest_cursor"] >= 1
    assert data["event_cursor"]["dropped_count"] == 0
    assert data["event_cursor"]["stale_cursor"] is False
    assert "runtime_smoke_evidence_bundle" in data["next_actions"]
    assert "runtime_smoke_run_plan" in data["next_actions"]


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_bundle_fails_closed_for_missing_run_id(
    capturing_mcp,
) -> None:
    session = RunPlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
        ctx=None,
        run_id="missing-run",
    )
    data = response["data"]

    assert data["status"] == "FAIL"
    assert data["reason"] == "runtime smoke run not found"
    assert data["run_id"] == "missing-run"
    assert data["final"] is True
    assert data["events"] == []
    assert data["result"] is None
    assert data["next_actions"] == ["runtime_smoke_run_plan"]
