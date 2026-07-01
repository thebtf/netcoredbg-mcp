from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_v2.actions.ui_drag import (
    REASON_NO_ROUTE_EVIDENCE,
)
from netcoredbg_mcp.session.runtime_smoke_v2.transition_executor import (
    _status_from_records,
)
from netcoredbg_mcp.ui.input_signature import RUNNER_INPUT_SIGNATURE


class ConfidenceSmokeSession:
    def __init__(
        self,
        monitor_result: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.monitor_result = (
            {"status": "PASS"} if monitor_result is None else monitor_result
        )
        self.monitor_calls: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.drag_calls: list[dict[str, Any]] = []
        self.drag_result: dict[str, Any] = {
            "status": "PASS",
            "route_evidence": {
                "move_points": [{"relative_to": "screen", "x": 10, "y": 20}],
                "final_pointer": {"relative_to": "screen", "x": 30, "y": 40},
            },
        }
        self.find_result: dict[str, Any] = {"status": "PASS", "found": True}
        self.focus_result: dict[str, Any] = {"status": "PASS"}
        self.send_keys_result: dict[str, Any] = {"status": "PASS"}
        self.text_get_state_result: dict[str, Any] = {
            "status": "PASS",
            "text": "Original text",
            "selection": {"start": 0, "end": 13, "length": 13},
            "selectionStart": 0,
            "selectionLength": 13,
        }
        self.text_read_result: dict[str, Any] = {
            "status": "PASS",
            "text": "Replaced text",
        }

    def input_monitor_check(self, **kwargs: Any) -> dict[str, Any]:
        self.monitor_calls.append(dict(kwargs))
        if isinstance(self.monitor_result, list):
            index = min(len(self.monitor_calls) - 1, len(self.monitor_result) - 1)
            return dict(self.monitor_result[index])
        return dict(self.monitor_result)

    def drag(self, **kwargs: Any) -> dict[str, Any]:
        self.drag_calls.append(dict(kwargs))
        return dict(self.drag_result)

    def find_element(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("find_element", dict(selector)))
        return {**self.find_result, "selector": dict(selector)}

    def set_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("set_focus", dict(selector)))
        return dict(self.focus_result)

    def send_keys_focused(self, keys: str) -> dict[str, Any]:
        self.calls.append(("send_keys_focused", keys))
        return {**self.send_keys_result, "sent": keys}

    def text_get_state(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("text_get_state", dict(selector)))
        return dict(self.text_get_state_result)

    def text_read(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("text_read", dict(selector)))
        return dict(self.text_read_result)


def _runner(
    session: ConfidenceSmokeSession,
    *,
    include_monitor: bool = True,
) -> RuntimeSmokeRunner:
    adapters = {
        "ui.drag": session.drag,
        "ui.find_element": session.find_element,
        "ui.set_focus": session.set_focus,
        "ui.send_keys_focused": session.send_keys_focused,
        "ui.text.get_state": session.text_get_state,
        "ui.text.read": session.text_read,
    }
    if include_monitor:
        adapters["runtime.input_monitor.check"] = session.input_monitor_check
    return RuntimeSmokeRunner(session, service_adapters=adapters)


def _no_operator_plan(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = {
        "schema": "netcoredbg.runtime_smoke.v2",
        "input_policy": {"no_global_input": True},
        "run_confidence": {"no_operator": True},
        "cases": [
            {
                "id": "no_operator_case",
                "transitions": [
                    {
                        "id": "noop_transition",
                        "action": {"kind": "noop"},
                        "probes": [],
                    }
                ],
            }
        ],
    }
    if extra:
        plan.update(extra)
    return plan


def _no_operator_drag_plan(*, no_global_input: bool) -> dict[str, Any]:
    return _no_operator_plan(
        {
            "input_policy": {"no_global_input": no_global_input},
            "cases": [
                {
                    "id": "drag_case",
                    "transitions": [
                        {
                            "id": "drag_transition",
                            "action": {
                                "kind": "ui.drag",
                                "source": {
                                    "point": {
                                        "relative_to": "screen",
                                        "x": 10,
                                        "y": 20,
                                    }
                                },
                                "path": [
                                    {
                                        "relative_to": "screen",
                                        "x": 15,
                                        "y": 25,
                                    }
                                ],
                                "drop": {
                                    "relative_to": "screen",
                                    "x": 30,
                                    "y": 40,
                                },
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )


def _no_operator_text_replace_plan(*, no_global_input: bool) -> dict[str, Any]:
    return _no_operator_plan(
        {
            "input_policy": {"no_global_input": no_global_input},
            "cases": [
                {
                    "id": "replace_text_case",
                    "transitions": [
                        {
                            "id": "replace_text_transition",
                            "action": {
                                "kind": "ui.text.type_replace_selection",
                                "selector": {"automation_id": "CueTextBox"},
                                "text": "Replaced text",
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )


def test_unknown_confidence_statuses_fail_closed_in_transition_aggregation() -> None:
    assert (
        _status_from_records([{"status": "PASS"}, {"status": "UNPROVEN"}]) == "BLOCKED"
    )


@pytest.mark.asyncio
async def test_no_operator_dirty_monitor_blocks_product_verdict() -> None:
    session = ConfidenceSmokeSession(
        {
            "status": "DIRTY",
            "source": "mouse",
            "window": "action",
            "summary": "external mouse movement observed",
        }
    )

    result = await _runner(session).run(_no_operator_plan())

    assert result["status"] == "BLOCKED"
    assert result["run_confidence"]["classification"] == "DIRTY_UNPROVEN"
    assert result["run_confidence"]["product_verdict_allowed"] is False
    assert result["run_confidence"]["contamination"]["source"] == "mouse"
    assert result["run_confidence"]["contamination"]["window"] == "action"
    assert "restart" in result["run_confidence"]["restart_guidance"].lower()
    assert result["compact"]["run_confidence"]["classification"] == "DIRTY_UNPROVEN"
    assert session.monitor_calls
    assert session.monitor_calls[0]["input_policy"] == {"no_global_input": True}
    assert session.monitor_calls[0]["run_confidence"] == {"no_operator": True}


@pytest.mark.asyncio
async def test_no_operator_dirty_after_action_blocks_product_verdict() -> None:
    session = ConfidenceSmokeSession(
        [
            {"status": "PASS", "basis": "external_input_monitor"},
            {
                "status": "DIRTY",
                "source": "keyboard",
                "window": "after_action",
                "summary": "external key press observed",
            },
        ]
    )

    result = await _runner(session).run(
        _no_operator_plan({"metrics_thresholds": {"action_latency_ms": {"max": 1}}})
    )

    assert result["status"] == "BLOCKED"
    assert result["run_confidence"]["classification"] == "DIRTY_UNPROVEN"
    assert result["run_confidence"]["contamination"]["source"] == "keyboard"
    assert result["run_confidence"]["contamination"]["window"] == "after_action"
    assert result["run_confidence"]["product_verdict_allowed"] is False
    assert [call["window"] for call in session.monitor_calls] == [
        "before_action",
        "after_action",
    ]


@pytest.mark.asyncio
async def test_no_operator_physical_input_inside_runner_drag_is_dirty_unproven() -> (
    None
):
    session = ConfidenceSmokeSession(
        [
            {
                "status": "PASS",
                "basis": "input_event_stream",
                "monitor": {"events": []},
            },
            {
                "status": "PASS",
                "basis": "input_event_stream",
                "window": "after_action",
                "monitor": {
                    "events": [
                        {"kind": "mouse", "injected": False, "source": "physical"}
                    ]
                },
            },
        ]
    )

    result = await _runner(session).run(_no_operator_drag_plan(no_global_input=False))

    confidence = result["run_confidence"]
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "operator input contaminated the scenario"
    assert confidence["classification"] == "DIRTY_UNPROVEN"
    assert confidence["product_verdict_allowed"] is False
    assert confidence["contamination"]["source"] == "physical"
    assert confidence["contamination"]["event"] == {
        "kind": "mouse",
        "injected": False,
        "source": "physical",
    }
    assert result["compact"]["run_confidence"]["classification"] == "DIRTY_UNPROVEN"
    assert session.drag_calls
    assert [call["window"] for call in session.monitor_calls] == [
        "before_action",
        "after_action",
    ]


@pytest.mark.asyncio
async def test_no_operator_foreign_injected_input_inside_runner_drag_is_dirty_unproven() -> (
    None
):
    session = ConfidenceSmokeSession(
        [
            {
                "status": "PASS",
                "basis": "input_event_stream",
                "monitor": {"events": []},
            },
            {
                "status": "PASS",
                "basis": "input_event_stream",
                "window": "after_action",
                "monitor": {
                    "events": [
                        {
                            "kind": "mouse",
                            "injected": True,
                            "extra_info": 123,
                            "source": "foreign_injected",
                        }
                    ]
                },
            },
        ]
    )

    result = await _runner(session).run(_no_operator_drag_plan(no_global_input=False))

    confidence = result["run_confidence"]
    assert result["status"] == "BLOCKED"
    assert confidence["classification"] == "DIRTY_UNPROVEN"
    assert confidence["contamination"]["source"] == "foreign_injected"
    assert "ambigu" not in str(confidence).lower()


@pytest.mark.asyncio
async def test_no_operator_runner_injected_drag_stays_clean_proven() -> None:
    session = ConfidenceSmokeSession(
        [
            {
                "status": "PASS",
                "basis": "input_event_stream",
                "monitor": {"events": []},
            },
            {
                "status": "PASS",
                "basis": "input_event_stream",
                "window": "after_action",
                "monitor": {
                    "events": [
                        {
                            "kind": "mouse",
                            "injected": True,
                            "extra_info": RUNNER_INPUT_SIGNATURE,
                            "source": "runner_injected",
                        }
                    ]
                },
            },
        ]
    )

    result = await _runner(session).run(_no_operator_drag_plan(no_global_input=False))

    confidence = result["run_confidence"]
    assert confidence["classification"] == "CLEAN_PROVEN"
    assert confidence["product_verdict_allowed"] is True
    assert result["compact"]["run_confidence"]["classification"] == "CLEAN_PROVEN"
    assert session.drag_calls


@pytest.mark.asyncio
async def test_no_operator_runner_signed_text_replace_stays_clean_proven() -> None:
    session = ConfidenceSmokeSession(
        [
            {
                "status": "PASS",
                "basis": "input_event_stream",
                "monitor": {"events": []},
            },
            {
                "status": "PASS",
                "basis": "input_event_stream",
                "window": "after_action",
                "monitor": {
                    "events": [
                        {
                            "kind": "keyboard",
                            "injected": True,
                            "extra_info": RUNNER_INPUT_SIGNATURE,
                            "source": "runner_injected",
                        }
                    ]
                },
            },
        ]
    )

    result = await _runner(session).run(
        _no_operator_text_replace_plan(no_global_input=False)
    )

    assert result["status"] == "PASS"
    confidence = result["run_confidence"]
    assert confidence["classification"] == "CLEAN_PROVEN"
    assert confidence["product_verdict_allowed"] is True
    assert session.calls == [
        ("find_element", {"automation_id": "CueTextBox"}),
        ("set_focus", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "^a"),
        ("text_get_state", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "Replaced text"),
        ("text_read", {"automation_id": "CueTextBox"}),
    ]


@pytest.mark.asyncio
async def test_no_operator_dirty_after_no_route_drag_remains_dirty_unproven() -> None:
    session = ConfidenceSmokeSession(
        [
            {"status": "PASS", "basis": "windows_last_input_info"},
            {
                "status": "DIRTY",
                "basis": "windows_last_input_info",
                "source": "global_input",
                "window": "after_action",
            },
        ]
    )
    session.drag_result = {"status": "PASS"}

    result = await _runner(session).run(_no_operator_drag_plan(no_global_input=False))

    assert result["status"] == "BLOCKED"
    assert result["reason"] == REASON_NO_ROUTE_EVIDENCE
    assert result["run_confidence"]["classification"] == "DIRTY_UNPROVEN"
    assert "runner_input" not in session.monitor_calls[1]
    assert result["compact"]["run_confidence"]["classification"] == "DIRTY_UNPROVEN"


@pytest.mark.asyncio
async def test_no_operator_external_dirty_after_successful_drag_stays_dirty() -> None:
    session = ConfidenceSmokeSession(
        [
            {"status": "PASS", "basis": "windows_last_input_info"},
            {
                "status": "DIRTY",
                "basis": "windows_last_input_info",
                "source": "keyboard",
                "window": "after_action",
            },
        ]
    )

    result = await _runner(session).run(_no_operator_drag_plan(no_global_input=False))

    confidence = result["run_confidence"]
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "operator input contaminated the scenario"
    assert confidence["classification"] == "DIRTY_UNPROVEN"
    assert confidence["contamination"]["source"] == "keyboard"
    assert result["compact"]["run_confidence"]["classification"] == "DIRTY_UNPROVEN"


@pytest.mark.asyncio
async def test_no_operator_runner_ambiguity_does_not_mask_drag_failure() -> None:
    session = ConfidenceSmokeSession(
        [
            {"status": "PASS", "basis": "windows_last_input_info"},
            {
                "status": "DIRTY",
                "basis": "windows_last_input_info",
                "source": "global_input",
                "window": "after_action",
            },
        ]
    )
    session.drag_result = {
        "status": "PASS",
        "route_evidence": {"move_points": [{"x": 1, "y": 1}]},
        "selected_payload": {"before": ["Cue 001"], "after": ["Cue 002"]},
    }
    plan = _no_operator_drag_plan(no_global_input=False)
    plan["cases"][0]["transitions"][0]["action"]["expect"] = {
        "selected_payload_preserved": True
    }

    result = await _runner(session).run(plan)

    assert result["status"] == "FAIL"
    assert result["reason"] == "selected payload expectation failed"
    assert result["run_confidence"]["classification"] == "DIRTY_UNPROVEN"


@pytest.mark.asyncio
async def test_no_operator_drag_still_blocks_when_no_global_input_is_required() -> None:
    session = ConfidenceSmokeSession({"status": "PASS"})

    result = await _runner(session).run(_no_operator_drag_plan(no_global_input=True))

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "global input prohibited by no_global_input policy"
    assert result["run_confidence"]["classification"] == "CLEAN_PROVEN"
    assert result["run_confidence"]["product_verdict_allowed"] is True
    assert session.drag_calls == []


@pytest.mark.asyncio
async def test_no_operator_dirty_before_runner_global_input_stays_operator_dirty() -> (
    None
):
    session = ConfidenceSmokeSession(
        {
            "status": "DIRTY",
            "basis": "windows_last_input_info",
            "source": "global_input",
            "window": "before_action",
            "summary": "Windows last-input tick advanced before action.",
        }
    )

    result = await _runner(session).run(_no_operator_drag_plan(no_global_input=False))

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "operator input contaminated the scenario"
    assert result["run_confidence"]["classification"] == "DIRTY_UNPROVEN"
    assert result["run_confidence"]["contamination"]["window"] == "before_action"
    assert session.drag_calls == []


@pytest.mark.asyncio
async def test_no_operator_missing_monitor_is_blocked_unproven() -> None:
    session = ConfidenceSmokeSession()

    result = await _runner(session, include_monitor=False).run(_no_operator_plan())

    assert result["status"] == "BLOCKED"
    assert result["run_confidence"]["classification"] == "UNPROVEN"
    assert result["run_confidence"]["basis"] == "monitor_unavailable"
    assert result["run_confidence"]["product_verdict_allowed"] is False
    assert "runtime.input_monitor.check" in result["run_confidence"]["restart_guidance"]
    assert result["compact"]["run_confidence"]["classification"] == "UNPROVEN"


@pytest.mark.asyncio
async def test_no_operator_malformed_monitor_is_blocked_unproven() -> None:
    session = ConfidenceSmokeSession({})

    result = await _runner(session).run(_no_operator_plan())

    assert result["status"] == "BLOCKED"
    assert result["run_confidence"]["classification"] == "UNPROVEN"
    assert result["run_confidence"]["basis"] == "monitor_malformed_result"
    assert result["run_confidence"]["product_verdict_allowed"] is False


@pytest.mark.asyncio
async def test_no_operator_missing_event_stream_stays_unproven() -> None:
    session = ConfidenceSmokeSession(
        {
            "status": "PASS",
            "window": "after_action",
            "basis": "input_event_stream",
            "monitor": {},
        }
    )

    result = await _runner(session).run(_no_operator_plan())

    assert result["status"] == "BLOCKED"
    assert result["run_confidence"]["classification"] == "UNPROVEN"
    assert result["run_confidence"]["basis"] == "monitor_not_observed"
    assert result["run_confidence"]["product_verdict_allowed"] is False


@pytest.mark.asyncio
async def test_no_operator_malformed_event_stream_fails_closed() -> None:
    session = ConfidenceSmokeSession(
        {
            "status": "PASS",
            "basis": "input_event_stream",
            "monitor": {"events": ["physical"]},
        }
    )

    result = await _runner(session).run(_no_operator_plan())

    assert result["status"] == "BLOCKED"
    assert result["run_confidence"]["classification"] == "DIRTY_UNPROVEN"
    assert result["run_confidence"]["contamination"]["source"] == "unattributable"
    assert result["run_confidence"]["product_verdict_allowed"] is False


@pytest.mark.asyncio
async def test_no_operator_clean_monitor_allows_product_failure() -> None:
    session = ConfidenceSmokeSession(
        {"status": "PASS", "basis": "external_input_monitor"}
    )

    result = await _runner(session).run(
        _no_operator_plan({"metrics_thresholds": {"action_latency_ms": {"max": 1}}})
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "metric threshold exceeded"
    assert result["run_confidence"]["classification"] == "CLEAN_PROVEN"
    assert result["run_confidence"]["product_verdict_allowed"] is True
    assert result["compact"]["run_confidence"]["classification"] == "CLEAN_PROVEN"
