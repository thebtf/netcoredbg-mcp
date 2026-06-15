"""Validate-only runtime-smoke plan facade tests."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class PlanFacadeSession:
    def __init__(self) -> None:
        self.launch_calls = 0
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.RUNNING,
            process_id=42,
            output_buffer=deque(),
        )
        self.process_registry = None

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        raise AssertionError("validate-only facade must not launch")


async def _resolve_project_root(_ctx: Any, _session: Any) -> None:
    raise AssertionError("validate-only facade must not resolve project paths")


def _register(capturing_mcp, session: PlanFacadeSession) -> None:
    register_runtime_smoke_tools(
        mcp=capturing_mcp,
        session=session,
        check_session_access=lambda ctx: None,
        resolve_project_root=_resolve_project_root,
    )


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_reports_invalid_v2_without_execution(
    capturing_mcp,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan={"schema": "netcoredbg.runtime_smoke.v2", "cases": "nope"},
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == ["cases must be a list"]
    assert data["accepted_schema_values"] == [
        "netcoredbg.runtime_smoke.v1",
        "netcoredbg.runtime_smoke.v2",
    ]
    assert "accepted_top_level_keys_v2" in data
    assert "accepted_action_kinds" in data
    assert "accepted_probe_kinds" in data
    assert session.launch_calls == 0
    assert "cleanup" not in data
    assert "completed_steps" not in data


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_reports_malformed_v2_case_without_exception(
    capturing_mcp,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan={"schema": "netcoredbg.runtime_smoke.v2", "cases": ["bad"]},
    )
    data = response["data"]

    assert "error" not in response
    assert data["status"] == "INVALID_SETUP"
    assert data["can_run"] is False
    assert data["validation_errors"] == ["cases[0] must be an object"]
    assert data["case_count"] == 0
    assert session.launch_calls == 0


@pytest.mark.asyncio
async def test_runtime_smoke_validate_plan_reports_runnable_v2_contract(
    capturing_mcp,
) -> None:
    session = PlanFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_validate_plan"](
        ctx=None,
        plan={
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "validate-only",
            "cases": [{"id": "case-1", "transitions": []}],
        },
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["can_run"] is True
    assert data["case_count"] == 1
    assert data["generated_case_count"] == 0
    assert data["validation_errors"] == []
    assert data["evidence_contract"]["result_keys"] == [
        "status",
        "reason",
        "elapsed_ms",
        "action_count",
        "cleanup",
        "evidence_refs",
        "compact",
    ]
    assert data["evidence_contract"]["diagnostics"]["schema"] == (
        "netcoredbg.runtime_smoke.diagnostics.v1"
    )
    assert data["evidence_contract"]["compact_limits"]["max_text_length"] == 240
    assert data["evidence_contract"]["compact_limits"]["max_list_items"] == 8
    assert session.launch_calls == 0
    assert "cleanup" not in data
    assert "completed_steps" not in data
