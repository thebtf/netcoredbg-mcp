"""Tests for CR-110 debuggee liveness telemetry."""

from unittest.mock import patch

import pytest

import netcoredbg_mcp.session.state as state_module
from netcoredbg_mcp.dap import DAPEvent
from netcoredbg_mcp.dap.events import StopReason
from netcoredbg_mcp.session import DebugState, SessionManager
from netcoredbg_mcp.session.state import SessionState, ThreadInfo


@pytest.mark.parametrize(
    ("debug_state", "stop_reason", "expected"),
    [
        (DebugState.RUNNING, None, "running"),
        (DebugState.STOPPED, StopReason.BREAKPOINT.value, "stopped-at-breakpoint"),
        (
            DebugState.STOPPED,
            StopReason.FUNCTION_BREAKPOINT.value,
            "stopped-at-breakpoint",
        ),
        (DebugState.STOPPED, StopReason.DATA_BREAKPOINT.value, "stopped-at-breakpoint"),
        (DebugState.STOPPED, StopReason.ENTRY.value, "stopped-at-breakpoint"),
        (DebugState.STOPPED, StopReason.EXCEPTION.value, "stopped-at-exception"),
        (DebugState.STOPPED, StopReason.STEP.value, "stepping"),
        (DebugState.STOPPED, StopReason.PAUSE.value, "stopped-at-pause"),
        (DebugState.STOPPED, StopReason.GOTO.value, "stopped-other"),
        (DebugState.TERMINATED, None, "terminated"),
        (DebugState.INITIALIZING, None, "initializing"),
        (DebugState.CONFIGURED, None, "configured"),
        (DebugState.APPLYING_CHANGES, None, "applying_changes"),
    ],
)
def test_derive_exec_state_covers_all_contract_branches(
    debug_state: DebugState,
    stop_reason: str | None,
    expected: str,
):
    derive_exec_state = getattr(state_module, "derive_exec_state")

    assert derive_exec_state(debug_state, stop_reason) == expected


def test_session_state_to_dict_exposes_exec_state_and_thread_count() -> None:
    state = SessionState(
        state=DebugState.STOPPED,
        stop_reason=StopReason.BREAKPOINT.value,
        threads=[ThreadInfo(id=1, name="Main")],
    )

    payload = state.to_dict()

    assert payload["execState"] == "stopped-at-breakpoint"
    assert payload["threadCount"] == 1


def test_session_state_to_dict_defaults_transition_timestamps_to_none() -> None:
    payload = SessionState().to_dict()

    assert payload["lastResumeAt"] is None
    assert payload["lastStopAt"] is None


def test_event_handlers_stamp_monotonic_transition_timestamps() -> None:
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()

    with patch(
        "netcoredbg_mcp.session.manager.time.monotonic",
        side_effect=[10.0, 20.0, 30.0],
    ):
        manager._on_continued(
            DAPEvent(
                seq=1,
                event="continued",
                body={"allThreadsContinued": True},
            )
        )
        manager._on_stopped(
            DAPEvent(
                seq=2,
                event="stopped",
                body={"reason": "breakpoint", "threadId": 1, "allThreadsStopped": True},
            )
        )
        manager._on_continued(
            DAPEvent(
                seq=3,
                event="continued",
                body={"allThreadsContinued": True},
            )
        )

    payload = manager.state.to_dict()

    assert payload["lastStopAt"] == pytest.approx(20.0)
    assert payload["lastResumeAt"] == pytest.approx(30.0)


def test_debuggee_liveness_fields_follow_process_and_state_transitions() -> None:
    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        manager = SessionManager()

    before_start = manager.state.to_dict()
    assert before_start["debuggeeAlive"] is False
    assert before_start["debuggeePid"] is None

    manager._on_process(
        DAPEvent(
            seq=1,
            event="process",
            body={"name": "SmokeTestApp", "systemProcessId": 4242},
        )
    )
    manager._on_continued(
        DAPEvent(
            seq=2,
            event="continued",
            body={"allThreadsContinued": True},
        )
    )

    running = manager.state.to_dict()
    assert running["debuggeeAlive"] is True
    assert running["debuggeePid"] == 4242
    assert running["execState"] == "running"

    manager._on_stopped(
        DAPEvent(
            seq=3,
            event="stopped",
            body={"reason": "exception", "threadId": 7, "allThreadsStopped": True},
        )
    )

    stopped = manager.state.to_dict()
    assert stopped["debuggeeAlive"] is True
    assert stopped["execState"] == "stopped-at-exception"

    manager._on_terminated(DAPEvent(seq=4, event="terminated", body={}))

    terminated = manager.state.to_dict()
    assert terminated["debuggeeAlive"] is False
    assert terminated["debuggeePid"] == 4242
    assert terminated["execState"] == "terminated"
