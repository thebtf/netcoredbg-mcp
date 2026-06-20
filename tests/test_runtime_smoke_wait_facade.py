"""Wait facade coverage for durable runtime-smoke runs."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class WaitFacadeSession:
    def __init__(self) -> None:
        self.launch_calls = 0
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.RUNNING,
            process_id=42,
            process_name="WaitFacade",
            output_buffer=deque(["ready: alpha\n"]),
            output_sequence=1,
            output_trimmed_before=0,
            modules=[],
            loaded_sources={},
        )
        self.process_registry = None

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        raise AssertionError("wait facade must not launch directly")


class NeverFinalRegistry:
    def __init__(self) -> None:
        self.start_calls = 0
        self.get_result_calls = 0
        self.tail_calls: list[dict[str, Any]] = []

    async def start(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.start_calls += 1
        raise AssertionError("wait facade must not create a run")

    async def get_result(self, run_id: str) -> dict[str, Any]:
        self.get_result_calls += 1
        return {
            "status": "RUNNING",
            "run_id": run_id,
            "plan_name": "slow-app-diagnostics",
            "lifecycle_status": "RUNNING",
            "final": False,
            "evidence_refs": [],
            "cleanup": None,
        }

    async def tail_events(
        self,
        run_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        self.tail_calls.append(
            {"run_id": run_id, "after_cursor": after_cursor, "limit": limit}
        )
        return {
            "status": "RUNNING",
            "run_id": run_id,
            "events": [
                {
                    "cursor": 1,
                    "kind": "started",
                    "status": "RUNNING",
                    "summary": "slow run still active",
                }
            ],
            "next_cursor": 1,
            "oldest_cursor": 1,
            "dropped_count": 0,
            "stale_cursor": False,
            "final": False,
        }


class CompletesBetweenResultAndTailRegistry:
    def __init__(self) -> None:
        self.get_result_calls = 0

    async def get_result(self, run_id: str) -> dict[str, Any]:
        self.get_result_calls += 1
        if self.get_result_calls == 1:
            return {
                "status": "RUNNING",
                "run_id": run_id,
                "plan_name": "race-completes",
                "lifecycle_status": "RUNNING",
                "final": False,
                "evidence_refs": [],
                "cleanup": None,
            }
        return {
            "status": "PASS",
            "run_id": run_id,
            "plan_name": "race-completes",
            "lifecycle_status": "COMPLETED",
            "final": True,
            "action_count": 1,
            "evidence_refs": ["race:final"],
            "cleanup": {"status": "PASS"},
        }

    async def tail_events(
        self,
        run_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return {
            "status": "COMPLETED",
            "run_id": run_id,
            "events": [
                {
                    "cursor": 1,
                    "kind": "completed",
                    "status": "PASS",
                    "summary": "race run completed",
                }
            ],
            "next_cursor": 1,
            "oldest_cursor": 1,
            "dropped_count": 0,
            "stale_cursor": False,
            "final": True,
        }


async def _resolve_project_root(_ctx: Any, _session: Any) -> None:
    raise AssertionError("wait facade test must not resolve project paths")


def _register(capturing_mcp, session: WaitFacadeSession) -> list[Any]:
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


@pytest.mark.asyncio
async def test_runtime_smoke_wait_for_result_returns_final_app_diagnostics_bundle(
    capturing_mcp,
) -> None:
    session = WaitFacadeSession()
    access_calls = _register(capturing_mcp, session)

    started = await capturing_mcp.tools["runtime_smoke_run_probe"](
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
        name="app-diagnostics-wait",
    )
    run_id = started["data"]["run_id"]

    waited = await capturing_mcp.tools["runtime_smoke_wait_for_result"](
        ctx=None,
        run_id=run_id,
        timeout_ms=500,
        poll_interval_ms=1,
        event_limit=10,
    )
    data = waited["data"]

    assert data["status"] == "BLOCKED"
    assert data["run_id"] == run_id
    assert data["final"] is True
    assert data["result"]["status"] == "BLOCKED"
    assert data["result"]["action_count"] == 1
    assert [event["kind"] for event in data["events"]] == ["started", "completed"]
    assert data["event_cursor"]["limit"] == 10
    assert "runtime_smoke_wait_for_result" in data["next_actions"]
    assert "runtime_smoke_evidence_bundle" in data["next_actions"]
    assert session.launch_calls == 0
    assert len(access_calls) >= 2


@pytest.mark.asyncio
async def test_runtime_smoke_event_delta_streams_wait_json_progress_before_case_completion(
    capturing_mcp,
    tmp_path,
) -> None:
    session = WaitFacadeSession()
    _register(capturing_mcp, session)
    missing_path = tmp_path / "pending-app-diagnostics.json"

    started = await capturing_mcp.tools["runtime_smoke_run_probe"](
        ctx=None,
        probe={
            "kind": "app_diagnostics",
            "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
            "app": {"name": "WpfSmokeApp", "process_name": "dotnet"},
            "status": "PASS",
            "observations": [],
            "redaction": {"omit_fields": ["raw_tree", "screenshot_base64", "secret"]},
            "limits": {
                "max_text_length": 240,
                "max_list_items": 8,
                "max_json_bytes": 32768,
            },
            "wait_json": {
                "path": str(missing_path),
                "timeout_ms": 500,
                "poll_interval_ms": 20,
            },
        },
        name="app-diagnostics-live-wait-json",
    )
    run_id = started["data"]["run_id"]

    mark = await capturing_mcp.tools["runtime_smoke_mark_event_cursor"](
        ctx=None,
        run_id=run_id,
        include_app_diagnostics=True,
    )
    assert mark["data"]["final"] is False
    assert mark["data"]["cursor"]["sources"]["app_diagnostics"] == {
        "after_index": 0,
        "entry_count": 0,
    }

    await asyncio.sleep(0.06)

    delta = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor=mark["data"]["cursor"],
        event_limit=10,
    )
    data = delta["data"]

    assert data["final"] is False
    app_diagnostics = data["source_deltas"]["app_diagnostics"]
    assert app_diagnostics["available"] >= 1
    assert app_diagnostics["entries"][0] == {
        "case_id": "run_probe",
        "transition_index": 0,
        "phase": "after",
        "probe": "app_diagnostics",
        "status": "RUNNING",
        "reason": "waiting for app_diagnostics.wait_json",
        "progress": {
            "field": "wait_json",
            "metadata": {
                "path": str(missing_path),
                "observed": False,
                "polls": 1,
                "timeout_ms": 500,
            },
        },
        "evidence_ref": "diagnostic:app_diagnostics:WpfSmokeApp",
    }
    assert data["cursor"]["sources"]["app_diagnostics"]["after_index"] >= 1

    await capturing_mcp.tools["runtime_smoke_wait_for_result"](
        ctx=None,
        run_id=run_id,
        timeout_ms=1000,
        poll_interval_ms=1,
        event_limit=10,
    )


@pytest.mark.asyncio
async def test_runtime_smoke_wait_for_result_times_out_with_latest_cursor(
    capturing_mcp,
) -> None:
    session = WaitFacadeSession()
    registry = NeverFinalRegistry()
    session.runtime_smoke.lifecycle_runs = registry
    access_calls = _register(capturing_mcp, session)

    waited = await asyncio.wait_for(
        capturing_mcp.tools["runtime_smoke_wait_for_result"](
            ctx=None,
            run_id="slow-run",
            timeout_ms=1,
            poll_interval_ms=1,
            after_cursor=0,
            event_limit=5,
        ),
        timeout=0.2,
    )
    data = waited["data"]

    assert data["status"] == "BLOCKED"
    assert data["reason"] == "runtime smoke wait timed out"
    assert data["run_id"] == "slow-run"
    assert data["final"] is False
    assert data["event_cursor"]["next_cursor"] == 1
    assert data["event_cursor"]["limit"] == 5
    assert data["events"][0]["kind"] == "started"
    assert data["next_step"] == (
        "Poll again with runtime_smoke_evidence_bundle or increase timeout_ms."
    )
    assert "runtime_smoke_wait_for_result" in data["next_actions"]
    assert "runtime_smoke_stop" in data["next_actions"]
    assert registry.start_calls == 0
    assert registry.get_result_calls >= 1
    assert registry.tail_calls
    assert session.launch_calls == 0
    assert len(access_calls) == 1


@pytest.mark.asyncio
async def test_runtime_smoke_wait_for_result_agent_mode_retry_advances_cursor(
    capturing_mcp,
) -> None:
    session = WaitFacadeSession()
    session.runtime_smoke.lifecycle_runs = NeverFinalRegistry()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_wait_for_result"](
        ctx=None,
        run_id="slow-run",
        timeout_ms=1,
        poll_interval_ms=1,
        after_cursor=0,
        event_limit=5,
        agent_mode=True,
    )
    data = response["data"]

    assert data["status"] == "BLOCKED"
    assert data["event_cursor"]["next_cursor"] == 1
    assert data["agent_mode"]["primary_next_action"] == "runtime_smoke_wait_for_result"
    assert data["agent_mode"]["next_request"] == {
        "tool": "runtime_smoke_wait_for_result",
        "arguments": {
            "run_id": "slow-run",
            "after_cursor": 1,
            "agent_mode": True,
            "event_limit": 20,
        },
    }


@pytest.mark.asyncio
async def test_runtime_smoke_wait_for_result_missing_run_fails_closed(
    capturing_mcp,
) -> None:
    session = WaitFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_wait_for_result"](
        ctx=None,
        run_id="missing-run",
        timeout_ms=100,
        poll_interval_ms=1,
    )
    data = response["data"]

    assert data["status"] == "FAIL"
    assert data["reason"] == "runtime smoke run not found"
    assert data["run_id"] == "missing-run"
    assert data["final"] is True
    assert data["events"] == []
    assert data["next_actions"] == ["runtime_smoke_run_plan"]


@pytest.mark.asyncio
async def test_runtime_smoke_wait_for_result_handles_completion_between_reads(
    capturing_mcp,
) -> None:
    session = WaitFacadeSession()
    registry = CompletesBetweenResultAndTailRegistry()
    session.runtime_smoke.lifecycle_runs = registry
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_wait_for_result"](
        ctx=None,
        run_id="race-run",
        timeout_ms=0,
        poll_interval_ms=1,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["final"] is True
    assert data["result"]["status"] == "PASS"
    assert data["result"]["action_count"] == 1
    assert data["events"][0]["kind"] == "completed"
    assert registry.get_result_calls == 2
