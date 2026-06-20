"""Event cursor/delta runtime-smoke facade tests."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState, OutputEntry
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class CursorFacadeRegistry:
    def __init__(self) -> None:
        self.tail_calls: list[dict[str, Any]] = []
        self.start_calls = 0

    async def start(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.start_calls += 1
        raise AssertionError("event cursor facades must not create runtime-smoke runs")

    async def get_result(self, run_id: str) -> dict[str, Any]:
        if run_id == "missing-run":
            return {
                "status": "FAIL",
                "reason": "runtime smoke run not found",
                "run_id": run_id,
            }
        return {
            "status": "RUNNING",
            "reason": "runtime smoke run still running",
            "run_id": run_id,
            "plan_name": "cursor-plan",
            "lifecycle_status": "RUNNING",
            "final": False,
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
        if run_id == "missing-run":
            return {
                "status": "FAIL",
                "reason": "runtime smoke run not found",
                "run_id": run_id,
            }
        if run_id == "renamed-missing-run":
            return {
                "status": "FAIL",
                "reason": "no retained runtime smoke run",
                "run_id": run_id,
            }
        if run_id == "stale-run":
            return {
                "status": "COMPLETED",
                "run_id": run_id,
                "events": [{"cursor": 7, "kind": "completed"}],
                "next_cursor": 7,
                "oldest_cursor": 5,
                "dropped_count": 4,
                "stale_cursor": True,
                "final": True,
            }
        if run_id == "failed-run":
            failed_events = [{"cursor": 8, "kind": "failed", "status": "FAIL"}]
            failed_events = [
                event for event in failed_events if int(event["cursor"]) > after_cursor
            ][:limit]
            if limit == 0:
                return {
                    "status": "FAIL",
                    "reason": "runtime smoke scenario failed",
                    "run_id": run_id,
                    "events": [],
                    "next_cursor": 8,
                    "oldest_cursor": 3,
                    "dropped_count": 1,
                    "stale_cursor": False,
                    "final": True,
                }
            return {
                "status": "FAIL",
                "reason": "runtime smoke scenario failed",
                "run_id": run_id,
                "events": failed_events,
                "next_cursor": 8,
                "oldest_cursor": 3,
                "dropped_count": 1,
                "stale_cursor": False,
                "final": True,
            }
        if run_id == "cursorless-run":
            return {
                "status": "COMPLETED",
                "run_id": run_id,
                "events": [{"kind": "progress", "status": "RUNNING"}],
                "next_cursor": 9,
                "oldest_cursor": 9,
                "dropped_count": 0,
                "stale_cursor": False,
                "final": True,
            }
        if run_id == "many-events-run":
            events = [
                {"cursor": cursor, "kind": "progress", "status": "RUNNING"}
                for cursor in range(1, 11)
            ]
            events = [event for event in events if int(event["cursor"]) > after_cursor][
                :limit
            ]
            return {
                "status": "COMPLETED",
                "run_id": run_id,
                "events": events,
                "next_cursor": 10,
                "oldest_cursor": 1,
                "dropped_count": 0,
                "stale_cursor": False,
                "final": True,
            }
        if limit == 0:
            return {
                "status": "RUNNING",
                "run_id": run_id,
                "events": [],
                "next_cursor": 4,
                "oldest_cursor": 1,
                "dropped_count": 0,
                "stale_cursor": False,
                "final": False,
            }
        events = [
            {"cursor": 5, "kind": "progress", "status": "RUNNING"},
            {"cursor": 6, "kind": "completed", "status": "COMPLETED"},
        ]
        events = [event for event in events if int(event["cursor"]) > after_cursor][:limit]
        return {
            "status": "COMPLETED",
            "run_id": run_id,
            "events": events,
            "next_cursor": 6,
            "oldest_cursor": 1,
            "dropped_count": 0,
            "stale_cursor": False,
            "final": True,
        }


class CursorFacadeSession:
    def __init__(self) -> None:
        self.launch_calls = 0
        self.runtime_smoke = RuntimeSmokeSession()
        self.runtime_smoke.lifecycle_runs = CursorFacadeRegistry()
        self.state = SimpleNamespace(
            state=DebugState.RUNNING,
            process_id=42,
            process_name="CursorFacade",
            output_buffer=deque(),
            output_sequence=0,
            output_trimmed_before=0,
            modules=[],
            loaded_sources={},
        )
        self.process_registry = None

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        raise AssertionError("event cursor facade must not launch directly")


async def _resolve_project_root(_ctx: Any, _session: Any) -> None:
    raise AssertionError("event cursor facade tests must not resolve project paths")


def _register(capturing_mcp, session: CursorFacadeSession) -> list[Any]:
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
async def test_runtime_smoke_mark_event_cursor_returns_cursor_token(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    access_calls = _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_mark_event_cursor"](
        ctx=None,
        run_id="run-1",
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["run_id"] == "run-1"
    assert data["cursor"] == {
        "run_id": "run-1",
        "after_cursor": 4,
        "next_cursor": 4,
        "oldest_cursor": 1,
        "dropped_count": 0,
        "stale_cursor": False,
    }
    assert data["final"] is False
    assert session.runtime_smoke.lifecycle_runs.tail_calls == [
        {"run_id": "run-1", "after_cursor": 0, "limit": 0}
    ]
    assert session.runtime_smoke.lifecycle_runs.start_calls == 0
    assert session.launch_calls == 0
    assert len(access_calls) == 1
    assert "runtime_smoke_get_event_delta" in response["next_actions"]


@pytest.mark.asyncio
async def test_runtime_smoke_mark_event_cursor_agent_mode_adds_delta_request(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_mark_event_cursor"](
        ctx=None,
        run_id="run-1",
        agent_mode=True,
    )
    data = response["data"]
    agent = data["agent_mode"]

    assert data["status"] == "PASS"
    assert agent["primary_next_action"] == "runtime_smoke_get_event_delta"
    assert agent["cursor"] == data["cursor"]
    assert agent["next_request"] == {
        "tool": "runtime_smoke_get_event_delta",
        "arguments": {
            "cursor": data["cursor"],
            "agent_mode": True,
            "event_limit": 20,
        },
    }


@pytest.mark.asyncio
async def test_runtime_smoke_mark_event_cursor_can_capture_debug_output_cursor(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    session.state.output_buffer = deque(
        [
            OutputEntry(text="before\n", category="stdout", sequence=1),
            OutputEntry(text="after\n", category="stderr", sequence=2),
        ]
    )
    session.state.output_sequence = 2
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_mark_event_cursor"](
        ctx=None,
        run_id="run-1",
        include_debug_output=True,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["cursor"]["sources"]["debug_output"] == {
        "after_sequence": 2,
        "trimmed_before": 0,
    }


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_returns_bounded_events_after_mark(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)
    cursor = {
        "run_id": "run-1",
        "after_cursor": 4,
        "next_cursor": 4,
        "oldest_cursor": 1,
        "dropped_count": 0,
        "stale_cursor": False,
    }

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor=cursor,
        event_limit=1,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["run_id"] == "run-1"
    assert data["events"] == [{"cursor": 5, "kind": "progress", "status": "RUNNING"}]
    assert data["event_cursor"] == {
        "after_cursor": 4,
        "next_cursor": 6,
        "oldest_cursor": 1,
        "dropped_count": 0,
        "stale_cursor": False,
        "limit": 1,
    }
    assert data["final"] is True
    assert data["cursor"]["after_cursor"] == 5
    assert "runtime_smoke_mark_event_cursor" in response["next_actions"]
    assert "runtime_smoke_evidence_bundle" in response["next_actions"]

    next_response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor=data["cursor"],
        event_limit=1,
    )

    assert next_response["data"]["events"] == [
        {"cursor": 6, "kind": "completed", "status": "COMPLETED"}
    ]
    assert next_response["data"]["cursor"]["after_cursor"] == 6


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_returns_debug_output_source_delta(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    session.state.output_buffer = deque(
        [
            OutputEntry(text="old\n", category="stdout", sequence=1),
            OutputEntry(text="new-1\n", category="stdout", sequence=2),
            OutputEntry(text="new-2\n", category="stderr", sequence=3),
        ]
    )
    session.state.output_sequence = 3
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={
            "run_id": "run-1",
            "after_cursor": 4,
            "sources": {
                "debug_output": {
                    "after_sequence": 1,
                    "trimmed_before": 0,
                }
            },
        },
        event_limit=1,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["events"] == [{"cursor": 5, "kind": "progress", "status": "RUNNING"}]
    assert data["source_deltas"]["debug_output"] == {
        "entries": [
            {
                "text": "new-1\n",
                "category": "stdout",
                "variables_reference": 0,
                "sequence": 2,
            }
        ],
        "available": 2,
        "limit": 1,
        "limited": True,
        "stale_cursor": False,
        "dropped_count": 0,
    }
    assert data["cursor"]["sources"]["debug_output"] == {
        "after_sequence": 2,
        "trimmed_before": 0,
    }


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_compacts_large_debug_output_entries(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    session.state.output_buffer = deque(
        [
            OutputEntry(text="x" * 500, category="stdout", sequence=2),
        ]
    )
    session.state.output_sequence = 2
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={
            "run_id": "run-1",
            "after_cursor": 4,
            "sources": {
                "debug_output": {
                    "after_sequence": 1,
                    "trimmed_before": 0,
                }
            },
        },
        event_limit=5,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["source_deltas"]["debug_output"]["entries"] == [
        {
            "text_length": 500,
            "category": "stdout",
            "variables_reference": 0,
            "sequence": 2,
            "omitted_fields": ["text"],
        }
    ]


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_marks_debug_output_cursor_stale(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    session.state.output_buffer = deque(
        [
            OutputEntry(text="retained\n", category="stdout", sequence=4),
        ]
    )
    session.state.output_sequence = 4
    session.state.output_trimmed_before = 3
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={
            "run_id": "run-1",
            "after_cursor": 4,
            "sources": {
                "debug_output": {
                    "after_sequence": 1,
                    "trimmed_before": 0,
                }
            },
        },
        event_limit=5,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["source_deltas"]["debug_output"] == {
        "entries": [
            {
                "text": "retained\n",
                "category": "stdout",
                "variables_reference": 0,
                "sequence": 4,
            }
        ],
        "available": 1,
        "limit": 5,
        "limited": False,
        "stale_cursor": True,
        "dropped_count": 2,
    }
    assert data["cursor"]["sources"]["debug_output"] == {
        "after_sequence": 4,
        "trimmed_before": 3,
    }


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_marks_cleared_debug_output_gap_stale(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    session.state.output_buffer = deque()
    session.state.output_sequence = 3
    session.state.output_trimmed_before = 0
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={
            "run_id": "run-1",
            "after_cursor": 4,
            "sources": {
                "debug_output": {
                    "after_sequence": 1,
                    "trimmed_before": 0,
                }
            },
        },
        event_limit=5,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["source_deltas"]["debug_output"] == {
        "entries": [],
        "available": 0,
        "limit": 5,
        "limited": False,
        "stale_cursor": True,
        "dropped_count": 2,
    }
    assert data["cursor"]["sources"]["debug_output"] == {
        "after_sequence": 1,
        "trimmed_before": 0,
    }


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_marks_cleared_gap_stale_before_retained_output(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    session.state.output_buffer = deque(
        [
            OutputEntry(text="retained-1\n", category="stdout", sequence=5),
            OutputEntry(text="retained-2\n", category="stderr", sequence=6),
        ]
    )
    session.state.output_sequence = 6
    session.state.output_trimmed_before = 0
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={
            "run_id": "run-1",
            "after_cursor": 4,
            "sources": {
                "debug_output": {
                    "after_sequence": 1,
                    "trimmed_before": 0,
                }
            },
        },
        event_limit=5,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["source_deltas"]["debug_output"] == {
        "entries": [
            {
                "text": "retained-1\n",
                "category": "stdout",
                "variables_reference": 0,
                "sequence": 5,
            },
            {
                "text": "retained-2\n",
                "category": "stderr",
                "variables_reference": 0,
                "sequence": 6,
            },
        ],
        "available": 2,
        "limit": 5,
        "limited": False,
        "stale_cursor": True,
        "dropped_count": 3,
    }
    assert data["cursor"]["sources"]["debug_output"] == {
        "after_sequence": 6,
        "trimmed_before": 0,
    }


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_zero_limit_does_not_advance_cursor(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={"run_id": "run-1", "after_cursor": 4},
        event_limit=0,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["events"] == []
    assert data["cursor"]["after_cursor"] == 4
    assert data["cursor"]["next_cursor"] == 4


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_cursorless_events_fall_back_to_tail_cursor(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={"run_id": "cursorless-run", "after_cursor": 4},
        event_limit=1,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["events"] == [{"kind": "progress", "status": "RUNNING"}]
    assert data["event_cursor"]["next_cursor"] == 9
    assert data["cursor"]["after_cursor"] == 9


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_returns_all_events_within_limit(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={"run_id": "many-events-run", "after_cursor": 0},
        event_limit=10,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert len(data["events"]) == 10
    assert [event["cursor"] for event in data["events"]] == list(range(1, 11))
    assert {"omitted_count": 2} not in data["events"]
    assert data["cursor"]["after_cursor"] == 10


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_preserves_stale_cursor_metadata(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={"run_id": "stale-run", "after_cursor": 1},
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["event_cursor"]["oldest_cursor"] == 5
    assert data["event_cursor"]["dropped_count"] == 4
    assert data["event_cursor"]["stale_cursor"] is True
    assert data["cursor"]["after_cursor"] == 7


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_preserves_retained_failed_run_events(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={"run_id": "failed-run", "after_cursor": 7},
        event_limit=2,
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["reason"] == "runtime smoke event delta read"
    assert data["run_id"] == "failed-run"
    assert data["events"] == [{"cursor": 8, "kind": "failed", "status": "FAIL"}]
    assert data["event_cursor"] == {
        "after_cursor": 7,
        "next_cursor": 8,
        "oldest_cursor": 3,
        "dropped_count": 1,
        "stale_cursor": False,
        "limit": 2,
    }
    assert data["final"] is True
    assert data["cursor"]["after_cursor"] == 8


@pytest.mark.asyncio
async def test_runtime_smoke_mark_event_cursor_preserves_retained_failed_run_cursor(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_mark_event_cursor"](
        ctx=None,
        run_id="failed-run",
    )
    data = response["data"]

    assert data["status"] == "PASS"
    assert data["reason"] == "runtime smoke event cursor marked"
    assert data["run_id"] == "failed-run"
    assert data["cursor"] == {
        "run_id": "failed-run",
        "after_cursor": 8,
        "next_cursor": 8,
        "oldest_cursor": 3,
        "dropped_count": 1,
        "stale_cursor": False,
    }
    assert data["final"] is True


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_missing_run_fails_closed(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={"run_id": "missing-run", "after_cursor": 2},
    )
    data = response["data"]

    assert data["status"] == "FAIL"
    assert data["reason"] == "runtime smoke run not found"
    assert data["run_id"] == "missing-run"
    assert data["events"] == []
    assert data["event_cursor"] == {
        "after_cursor": 2,
        "next_cursor": 2,
        "oldest_cursor": None,
        "dropped_count": 0,
        "stale_cursor": False,
        "limit": 50,
    }
    assert data["final"] is True
    assert "runtime_smoke_run_plan" in response["next_actions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["not-a-cursor", {}])
async def test_runtime_smoke_get_event_delta_contextless_invalid_cursor_omits_mark_action(
    capturing_mcp,
    cursor,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor=cursor,
    )
    data = response["data"]

    assert data["status"] == "INVALID_SETUP"
    assert "runtime_smoke_run_plan" in response["next_actions"]
    assert "runtime_smoke_mark_event_cursor" not in response["next_actions"]


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_missing_run_without_literal_reason_fails_closed(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={"run_id": "renamed-missing-run", "after_cursor": 2},
    )
    data = response["data"]

    assert data["status"] == "FAIL"
    assert data["reason"] == "no retained runtime smoke run"
    assert data["events"] == []
    assert data["next_actions"] == ["runtime_smoke_run_plan"]


@pytest.mark.asyncio
async def test_runtime_smoke_mark_event_cursor_missing_run_without_literal_reason_fails_closed(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_mark_event_cursor"](
        ctx=None,
        run_id="renamed-missing-run",
    )
    data = response["data"]

    assert data["status"] == "FAIL"
    assert data["reason"] == "no retained runtime smoke run"
    assert data["events"] == []
    assert data["next_actions"] == ["runtime_smoke_run_plan"]


@pytest.mark.asyncio
async def test_runtime_smoke_mark_event_cursor_agent_mode_missing_run_has_no_delta_request(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_mark_event_cursor"](
        ctx=None,
        run_id="missing-run",
        agent_mode=True,
    )
    data = response["data"]
    agent = data["agent_mode"]

    assert data["status"] == "FAIL"
    assert agent["primary_next_action"] == "runtime_smoke_run_plan"
    assert "next_request" not in agent
    assert "cursor" not in agent


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_agent_mode_malformed_cursor_has_no_delta_request(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={},
        agent_mode=True,
    )
    data = response["data"]
    agent = data["agent_mode"]

    assert data["status"] == "INVALID_SETUP"
    assert agent["primary_next_action"] == "runtime_smoke_run_plan"
    assert "next_request" not in agent
    assert "cursor" not in agent


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_agent_mode_invalid_cursor_with_run_id_repairs_via_mark(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={"run_id": "run-1", "after_cursor": "bad"},
        agent_mode=True,
    )
    data = response["data"]
    agent = data["agent_mode"]

    assert data["status"] == "INVALID_SETUP"
    assert agent["primary_next_action"] == "runtime_smoke_mark_event_cursor"
    assert agent["next_request"] == {
        "tool": "runtime_smoke_mark_event_cursor",
        "arguments": {
            "run_id": "run-1",
            "agent_mode": True,
        },
    }
    assert "cursor" not in agent


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_agent_mode_non_object_cursor_stays_fail_closed(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor="not-a-cursor",
        agent_mode=True,
    )
    data = response["data"]
    agent = data["agent_mode"]

    assert data["status"] == "INVALID_SETUP"
    assert agent["primary_next_action"] == "runtime_smoke_run_plan"
    assert "next_request" not in agent
    assert "cursor" not in agent


@pytest.mark.asyncio
async def test_runtime_smoke_get_event_delta_agent_mode_missing_run_has_no_delta_request(
    capturing_mcp,
) -> None:
    session = CursorFacadeSession()
    _register(capturing_mcp, session)

    response = await capturing_mcp.tools["runtime_smoke_get_event_delta"](
        ctx=None,
        cursor={"run_id": "missing-run", "after_cursor": 2},
        agent_mode=True,
    )
    data = response["data"]
    agent = data["agent_mode"]

    assert data["status"] == "FAIL"
    assert agent["primary_next_action"] == "runtime_smoke_run_plan"
    assert "next_request" not in agent
    assert "cursor" not in agent
