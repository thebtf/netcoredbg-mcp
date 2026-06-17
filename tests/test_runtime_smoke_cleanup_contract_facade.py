"""Runtime-smoke cleanup contamination contract facade tests."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import (
    RuntimeSmokeRunner,
    RuntimeSmokeRunRegistry,
    RuntimeSmokeSession,
)
from netcoredbg_mcp.session.state import DebugState
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class CleanupContractFacadeSession:
    def __init__(self) -> None:
        self.cleanup_calls: list[str] = []
        self.launch_calls = 0
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.RUNNING,
            process_id=42,
            process_name="CleanupContractFacade",
            output_buffer=deque(),
            output_sequence=0,
            output_trimmed_before=0,
            modules=[],
            loaded_sources={},
        )
        self.process_registry = None
        self.release_event = asyncio.Event()
        self.cleanup_release_event = asyncio.Event()
        self.block_cleanup = False
        self.fail_cleanup = False

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        raise AssertionError("cleanup contract facade must not launch directly")

    async def wait_until_released(self) -> dict[str, Any]:
        await self.release_event.wait()
        return {"status": "PASS", "reason": "released"}

    async def clear_group(self, name: str) -> dict[str, Any]:
        self.cleanup_calls.append(name)
        if self.block_cleanup:
            await self.cleanup_release_event.wait()
        if self.fail_cleanup:
            raise RuntimeError("cleanup exploded")
        self.runtime_smoke.instrumentation_groups.pop(name, None)
        return {"status": "PASS", "reason": "instrumentation group cleared"}


async def _resolve_project_root(_ctx: Any, _session: Any) -> None:
    raise AssertionError("cleanup contract facade tests must not resolve project paths")


def _runner(session: CleanupContractFacadeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "wait_until_released": session.wait_until_released,
            "instrumentation_group_clear": session.clear_group,
        },
    )


async def _wait_for_final(registry: RuntimeSmokeRunRegistry, run_id: str) -> dict[str, Any]:
    for _ in range(20):
        result = await registry.get_result(run_id)
        if result.get("final"):
            return result
        await asyncio.sleep(0)
    raise AssertionError("runtime smoke lifecycle run did not finish")


async def _wait_for_evidence_bundle(capturing_mcp, run_id: str) -> dict[str, Any]:
    for _ in range(20):
        response = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
            ctx=None,
            run_id=run_id,
        )
        if response["data"].get("final"):
            return response
        await asyncio.sleep(0)
    raise AssertionError("runtime smoke evidence bundle did not finish")


def _register(capturing_mcp, session: CleanupContractFacadeSession) -> list[Any]:
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
async def test_runtime_smoke_cleanup_contract_clears_contamination_after_reset(
    capturing_mcp,
) -> None:
    session = CleanupContractFacadeSession()
    access_calls = _register(capturing_mcp, session)
    session.runtime_smoke.register_cleanup(
        "release-modifier",
        lambda: session.cleanup_calls.append("release-modifier"),
    )
    session.runtime_smoke.lifecycle_runs.mark_contaminated(
        reason="runtime smoke stop cleanup failed",
        run_id="run-1",
        cleanup={"status": "FAIL", "failures": [{"reason": "cleanup exploded"}]},
    )

    response = await capturing_mcp.tools["runtime_smoke_cleanup_contract"](
        ctx=None,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["reason"] == "runtime smoke cleanup contract satisfied"
    assert data["contaminated"] is False
    assert data["cleanup_contract"]["status"] == "PASS"
    assert data["cleanup_contract"]["required_before"] is True
    assert "runtime_smoke_reset" in data["cleanup_contract"]["attempted"]
    assert session.cleanup_calls == ["release-modifier"]
    assert session.runtime_smoke.lifecycle_runs.contamination() is None
    assert "runtime_smoke_run_plan" in response["next_actions"]
    assert len(access_calls) == 1


@pytest.mark.asyncio
async def test_runtime_smoke_cleanup_contract_is_idempotent_when_clean(
    capturing_mcp,
) -> None:
    session = CleanupContractFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_cleanup_contract"](
        ctx=None,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["reason"] == "runtime smoke cleanup contract already clean"
    assert data["contaminated"] is False
    assert data["cleanup_contract"]["status"] == "PASS"
    assert data["cleanup_contract"]["required_before"] is False
    assert data["cleanup_contract"]["attempted"] == []
    assert session.cleanup_calls == []


@pytest.mark.asyncio
async def test_runtime_smoke_cleanup_contract_failure_keeps_cleanup_as_next_action(
    capturing_mcp,
) -> None:
    session = CleanupContractFacadeSession()
    _register(capturing_mcp, session)

    def fail_cleanup() -> None:
        raise RuntimeError("release failed")

    session.runtime_smoke.register_cleanup("release-modifier", fail_cleanup)
    session.runtime_smoke.lifecycle_runs.mark_contaminated(
        reason="runtime smoke stop cleanup failed",
        run_id="run-1",
        cleanup={"status": "FAIL", "failures": [{"reason": "cleanup exploded"}]},
    )

    response = await capturing_mcp.tools["runtime_smoke_cleanup_contract"](
        ctx=None,
    )
    data = response["data"]

    assert data["status"] == "FAIL"
    assert data["contaminated"] is True
    assert "runtime_smoke_cleanup_contract" in response["next_actions"]
    assert "runtime_smoke_run_plan" not in response["next_actions"]
    assert "runtime_smoke_start" not in response["next_actions"]


@pytest.mark.asyncio
async def test_runtime_smoke_stop_points_contaminated_runs_at_cleanup_contract(
    capturing_mcp,
) -> None:
    session = CleanupContractFacadeSession()
    session.block_cleanup = True
    session.runtime_smoke.lifecycle_runs = RuntimeSmokeRunRegistry(stop_timeout_seconds=0.01)
    session.runtime_smoke.instrumentation_groups["flow"] = {"breakpoints": [1]}
    _register(capturing_mcp, session)
    started = await session.runtime_smoke.lifecycle_runs.start(
        {
            "name": "cleanup-timeout-facade",
            "actions": [{"name": "wait_until_released"}],
            "teardown": {"instrumentation_groups": ["flow"]},
        },
        lambda: _runner(session),
    )

    response = await capturing_mcp.tools["runtime_smoke_stop"](
        ctx=None,
        run_id=started["run_id"],
    )
    data = response["data"]

    assert data["status"] == "STOPPING"
    assert data["contaminated"] is True
    assert data["cleanup_contract"]["next_action"] == "runtime_smoke_cleanup_contract"
    assert "runtime_smoke_wait_for_result" in response["next_actions"]
    assert "runtime_smoke_evidence_bundle" in response["next_actions"]
    assert "runtime_smoke_tail_events" in response["next_actions"]
    assert "runtime_smoke_get_result" in response["next_actions"]
    assert "runtime_smoke_stop" in response["next_actions"]
    assert "runtime_smoke_cleanup_contract" in response["next_actions"]
    assert "debug_hygiene_preflight" in response["next_actions"]

    session.cleanup_release_event.set()
    await _wait_for_final(session.runtime_smoke.lifecycle_runs, started["run_id"])


@pytest.mark.asyncio
async def test_runtime_smoke_running_contaminated_surfaces_keep_stop_route(
    capturing_mcp,
) -> None:
    session = CleanupContractFacadeSession()
    session.runtime_smoke.instrumentation_groups["flow"] = {"breakpoints": [1]}
    _register(capturing_mcp, session)
    started = await session.runtime_smoke.lifecycle_runs.start(
        {
            "name": "running-contaminated",
            "actions": [{"name": "wait_until_released"}],
            "teardown": {"instrumentation_groups": ["flow"]},
        },
        lambda: _runner(session),
    )
    run_id = started["run_id"]
    session.runtime_smoke.lifecycle_runs.mark_contaminated(
        reason="external contamination while active",
        run_id=run_id,
        cleanup={"status": "FAIL", "failures": [{"reason": "external"}]},
    )

    tail = await capturing_mcp.tools["runtime_smoke_tail_events"](
        ctx=None,
        run_id=run_id,
    )
    result = await capturing_mcp.tools["runtime_smoke_get_result"](
        ctx=None,
        run_id=run_id,
    )

    for response in (tail, result):
        assert response["data"]["status"] == "RUNNING"
        assert response["data"]["contaminated"] is True
        assert "runtime_smoke_wait_for_result" in response["next_actions"]
        assert "runtime_smoke_evidence_bundle" in response["next_actions"]
        assert "runtime_smoke_tail_events" in response["next_actions"]
        assert "runtime_smoke_get_result" in response["next_actions"]
        assert "runtime_smoke_stop" in response["next_actions"]
        assert "runtime_smoke_cleanup_contract" in response["next_actions"]
        assert "debug_hygiene_preflight" in response["next_actions"]

    session.release_event.set()
    await _wait_for_final(session.runtime_smoke.lifecycle_runs, run_id)


@pytest.mark.asyncio
async def test_runtime_smoke_stopping_surfaces_preserve_cleanup_guidance(
    capturing_mcp,
) -> None:
    session = CleanupContractFacadeSession()
    session.block_cleanup = True
    session.runtime_smoke.lifecycle_runs = RuntimeSmokeRunRegistry(stop_timeout_seconds=0.01)
    session.runtime_smoke.instrumentation_groups["flow"] = {"breakpoints": [1]}
    _register(capturing_mcp, session)
    started = await session.runtime_smoke.lifecycle_runs.start(
        {
            "name": "cleanup-timeout-all-surfaces",
            "actions": [{"name": "wait_until_released"}],
            "teardown": {"instrumentation_groups": ["flow"]},
        },
        lambda: _runner(session),
    )
    run_id = started["run_id"]

    stop = await capturing_mcp.tools["runtime_smoke_stop"](
        ctx=None,
        run_id=run_id,
    )
    get_result = await capturing_mcp.tools["runtime_smoke_get_result"](
        ctx=None,
        run_id=run_id,
    )
    tail = await capturing_mcp.tools["runtime_smoke_tail_events"](
        ctx=None,
        run_id=run_id,
    )
    bundle = await capturing_mcp.tools["runtime_smoke_evidence_bundle"](
        ctx=None,
        run_id=run_id,
    )
    wait = await capturing_mcp.tools["runtime_smoke_wait_for_result"](
        ctx=None,
        run_id=run_id,
        timeout_ms=0,
    )

    for response in (stop, get_result, tail, bundle, wait):
        data = response["data"]
        assert data["run_id"] == run_id
        assert data["contaminated"] is True
        assert data["cleanup_contract"]["next_action"] == "runtime_smoke_cleanup_contract"
        assert "runtime_smoke_cleanup_contract" in response["next_actions"]
        assert "debug_hygiene_preflight" in response["next_actions"]

    assert stop["data"]["status"] == "STOPPING"
    assert get_result["data"]["status"] == "STOPPING"
    assert tail["data"]["status"] == "STOPPING"
    assert bundle["data"]["status"] == "STOPPING"
    assert wait["data"]["status"] == "BLOCKED"
    assert wait["data"]["reason"] == "runtime smoke wait timed out"

    session.cleanup_release_event.set()
    await _wait_for_final(session.runtime_smoke.lifecycle_runs, run_id)


@pytest.mark.asyncio
async def test_runtime_smoke_evidence_and_wait_preserve_contamination_guidance(
    capturing_mcp,
) -> None:
    session = CleanupContractFacadeSession()
    session.fail_cleanup = True
    session.runtime_smoke.instrumentation_groups["flow"] = {"breakpoints": [1]}
    _register(capturing_mcp, session)
    started = await session.runtime_smoke.lifecycle_runs.start(
        {
            "name": "cleanup-failure-evidence",
            "actions": [{"name": "wait_until_released"}],
            "teardown": {"instrumentation_groups": ["flow"]},
        },
        lambda: _runner(session),
    )

    session.release_event.set()
    bundle = await _wait_for_evidence_bundle(capturing_mcp, started["run_id"])
    wait = await capturing_mcp.tools["runtime_smoke_wait_for_result"](
        ctx=None,
        run_id=started["run_id"],
        timeout_ms=100,
    )
    result = await capturing_mcp.tools["runtime_smoke_get_result"](
        ctx=None,
        run_id=started["run_id"],
    )
    tail = await capturing_mcp.tools["runtime_smoke_tail_events"](
        ctx=None,
        run_id=started["run_id"],
    )

    assert bundle["data"]["contaminated"] is True
    assert bundle["data"]["cleanup_contract"]["next_action"] == "runtime_smoke_cleanup_contract"
    assert "runtime_smoke_cleanup_contract" in bundle["next_actions"]
    assert "runtime_smoke_run_plan" not in bundle["next_actions"]
    assert wait["data"]["contaminated"] is True
    assert wait["data"]["cleanup_contract"]["next_action"] == "runtime_smoke_cleanup_contract"
    assert "runtime_smoke_cleanup_contract" in wait["next_actions"]
    assert "runtime_smoke_run_plan" not in wait["next_actions"]
    assert result["data"]["contaminated"] is True
    assert result["data"]["cleanup_contract"]["next_action"] == "runtime_smoke_cleanup_contract"
    assert "runtime_smoke_cleanup_contract" in result["next_actions"]
    assert "runtime_smoke_run_plan" not in result["next_actions"]
    assert tail["data"]["contaminated"] is True
    assert tail["data"]["cleanup_contract"]["next_action"] == "runtime_smoke_cleanup_contract"
    assert "runtime_smoke_cleanup_contract" in tail["next_actions"]
    assert "runtime_smoke_run_plan" not in tail["next_actions"]
