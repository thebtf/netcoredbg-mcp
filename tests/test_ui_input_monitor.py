from __future__ import annotations

from typing import Any

from netcoredbg_mcp.ui.input_monitor import (
    InputMonitorUnavailableError,
    LastInputSample,
    RuntimeInputMonitor,
)


def _kwargs(window: str) -> dict[str, Any]:
    return {
        "case_id": "case-1",
        "transition_id": "transition-1",
        "transition_index": 0,
        "window": window,
        "input_policy": {"no_global_input": True},
        "run_confidence": {"no_operator": True},
    }


def test_runtime_input_monitor_reports_clean_when_last_input_tick_is_stable() -> None:
    samples = iter(
        [
            LastInputSample(last_input_tick_ms=100, current_tick_ms=1000),
            LastInputSample(last_input_tick_ms=100, current_tick_ms=1100),
        ]
    )
    monitor = RuntimeInputMonitor(reader=lambda: next(samples))

    before = monitor.check(**_kwargs("before_action"))
    after = monitor.check(**_kwargs("after_action"))

    assert before["status"] == "PASS"
    assert before["basis"] == "windows_last_input_info"
    assert after["status"] == "PASS"
    assert after["basis"] == "windows_last_input_info"
    assert after["monitor"]["baseline"]["last_input_tick_ms"] == 100
    assert after["monitor"]["current"]["last_input_tick_ms"] == 100


def test_runtime_input_monitor_reports_dirty_when_last_input_tick_advances() -> None:
    samples = iter(
        [
            LastInputSample(last_input_tick_ms=100, current_tick_ms=1000),
            LastInputSample(last_input_tick_ms=220, current_tick_ms=1100),
        ]
    )
    monitor = RuntimeInputMonitor(reader=lambda: next(samples))

    monitor.check(**_kwargs("before_action"))
    after = monitor.check(**_kwargs("after_action"))

    assert after["status"] == "DIRTY"
    assert after["basis"] == "windows_last_input_info"
    assert after["source"] == "global_input"
    assert after["window"] == "after_action"
    assert "advanced" in after["summary"]


def test_runtime_input_monitor_reports_dirty_between_transition_windows() -> None:
    samples = iter(
        [
            LastInputSample(last_input_tick_ms=100, current_tick_ms=1000),
            LastInputSample(last_input_tick_ms=100, current_tick_ms=1100),
            LastInputSample(last_input_tick_ms=180, current_tick_ms=1200),
        ]
    )
    monitor = RuntimeInputMonitor(reader=lambda: next(samples))

    monitor.check(**_kwargs("before_action"))
    monitor.check(**_kwargs("after_action"))
    result = monitor.check(
        **{
            **_kwargs("before_action"),
            "transition_id": "transition-2",
            "transition_index": 1,
        }
    )

    assert result["status"] == "DIRTY"
    assert result["basis"] == "windows_last_input_info"
    assert result["window"] == "before_action"
    assert "between monitored windows" in result["summary"]
    assert result["monitor"]["baseline"]["last_input_tick_ms"] == 100
    assert result["monitor"]["current"]["last_input_tick_ms"] == 180


def test_runtime_input_monitor_blocks_when_backend_is_unavailable() -> None:
    monitor = RuntimeInputMonitor(
        reader=lambda: (_ for _ in ()).throw(
            InputMonitorUnavailableError("GetLastInputInfo unavailable")
        )
    )

    result = monitor.check(**_kwargs("before_action"))

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "GetLastInputInfo unavailable"
    assert result["basis"] == "windows_last_input_info"


def test_runtime_input_monitor_blocks_unsupported_window_before_reading() -> None:
    monitor = RuntimeInputMonitor(
        reader=lambda: (_ for _ in ()).throw(AssertionError("reader should not run"))
    )

    result = monitor.check(**{**_kwargs("unknown"), "window": "during_action"})

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "input monitor unsupported window"
    assert result["window"] == "during_action"


def test_runtime_input_monitor_blocks_missing_case_identity_before_reading() -> None:
    monitor = RuntimeInputMonitor(
        reader=lambda: (_ for _ in ()).throw(AssertionError("reader should not run"))
    )

    result = monitor.check(**{**_kwargs("before_action"), "case_id": ""})

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "input monitor missing case identity"


def test_runtime_input_monitor_blocks_missing_transition_identity_before_reading() -> None:
    monitor = RuntimeInputMonitor(
        reader=lambda: (_ for _ in ()).throw(AssertionError("reader should not run"))
    )

    result = monitor.check(
        **{
            **_kwargs("before_action"),
            "transition_id": None,
            "transition_index": None,
        }
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "input monitor missing transition identity"


def test_runtime_input_monitor_blocks_when_tick_regresses() -> None:
    samples = iter(
        [
            LastInputSample(last_input_tick_ms=500, current_tick_ms=1000),
            LastInputSample(last_input_tick_ms=450, current_tick_ms=1100),
        ]
    )
    monitor = RuntimeInputMonitor(reader=lambda: next(samples))

    monitor.check(**_kwargs("before_action"))
    result = monitor.check(**_kwargs("after_action"))

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "input monitor tick regressed"
    assert result["basis"] == "windows_last_input_info"


def test_runtime_input_monitor_blocks_after_action_without_baseline() -> None:
    monitor = RuntimeInputMonitor(
        reader=lambda: LastInputSample(last_input_tick_ms=100, current_tick_ms=1000)
    )

    result = monitor.check(**_kwargs("after_action"))

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "input monitor missing baseline"
