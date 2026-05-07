"""Runtime smoke session lifecycle tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState, EvidenceRef


def test_runtime_smoke_session_reset_clears_owned_state() -> None:
    smoke = RuntimeSmokeSession()
    smoke.instrumentation_groups["group"] = {"breakpoints": [1]}
    smoke.output_checkpoints["start"] = 10
    smoke.evidence_refs.append(EvidenceRef(kind="output", ref="output:1", summary="one line"))

    failures = smoke.reset()

    assert failures == ()
    assert smoke.instrumentation_groups == {}
    assert smoke.output_checkpoints == {}
    assert smoke.evidence_refs == []


def test_runtime_smoke_session_runs_cleanup_callbacks_during_reset() -> None:
    smoke = RuntimeSmokeSession()
    calls: list[str] = []

    smoke.register_cleanup("release-modifier", lambda: calls.append("released"))

    failures = smoke.reset()

    assert failures == ()
    assert calls == ["released"]


def test_runtime_smoke_session_records_cleanup_failure_without_leaking_state() -> None:
    smoke = RuntimeSmokeSession()
    smoke.instrumentation_groups["group"] = {"breakpoints": [1]}

    def fail_cleanup() -> None:
        raise RuntimeError("release failed")

    smoke.register_cleanup("release-modifier", fail_cleanup)

    failures = smoke.reset()

    assert failures == ({"name": "release-modifier", "error": "release failed"},)
    assert smoke.last_reset_failures == failures
    assert smoke.instrumentation_groups == {}
    assert smoke.output_checkpoints == {}


@pytest.mark.asyncio
async def test_session_manager_stop_resets_runtime_smoke_state(mock_netcoredbg_path) -> None:
    from netcoredbg_mcp.session import SessionManager

    manager = SessionManager()
    manager.runtime_smoke.instrumentation_groups["group"] = {"breakpoints": [1]}
    manager.runtime_smoke.output_checkpoints["start"] = 10
    manager._state.state = DebugState.STOPPED

    result = await manager.stop()

    assert result == {"success": True}
    assert manager.state.state == DebugState.IDLE
    assert manager.runtime_smoke.instrumentation_groups == {}
    assert manager.runtime_smoke.output_checkpoints == {}


def test_runtime_smoke_state_does_not_cross_session_managers(mock_netcoredbg_path) -> None:
    from netcoredbg_mcp.session import SessionManager

    with patch("netcoredbg_mcp.session.manager.DAPClient"):
        first = SessionManager()
        second = SessionManager()

    first.runtime_smoke.instrumentation_groups["group"] = {"breakpoints": [1]}

    assert second.runtime_smoke.instrumentation_groups == {}
    assert first.runtime_smoke is not second.runtime_smoke
