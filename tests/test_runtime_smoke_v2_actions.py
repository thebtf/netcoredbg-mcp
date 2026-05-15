from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession


class ActionSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.calls: list[tuple[str, Any]] = []
        self.tracepoint_hits: list[bool] = []
        self.focus_result: dict[str, Any] = {"status": "PASS"}
        self.send_keys_result: dict[str, Any] = {"status": "PASS"}
        self.drag_results: list[dict[str, Any]] = []

    async def find_element(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("find_element", dict(selector)))
        return {"status": "PASS", "found": True, "selector": dict(selector)}

    async def set_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("set_focus", dict(selector)))
        return dict(self.focus_result)

    async def send_keys_focused(self, keys: str) -> dict[str, Any]:
        self.calls.append(("send_keys_focused", keys))
        return {**self.send_keys_result, "sent": keys}

    async def invoke(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("invoke", dict(selector)))
        return {"status": "PASS", "method": "InvokePattern"}

    async def drag(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("drag", request))
        if self.drag_results:
            return self.drag_results.pop(0)
        return {
            "status": "PASS",
            "backend": "fake",
            "route_evidence": {
                "move_points": list(request.get("path") or []),
                "final_pointer": request.get("drop"),
            },
        }

    async def tracepoint_status(self, tracepoint_id: str) -> dict[str, Any]:
        self.calls.append(("tracepoint_status", tracepoint_id))
        hit = self.tracepoint_hits.pop(0) if self.tracepoint_hits else False
        return {"status": "PASS", "hit": hit}


class ManualClock:
    def __init__(self) -> None:
        self.current = 0.0
        self.sleeps_ms: list[int] = []

    def __call__(self) -> float:
        return self.current

    async def sleep_ms(self, idle_ms: int) -> None:
        self.sleeps_ms.append(idle_ms)
        self.current += idle_ms / 1000


def _runner(session: ActionSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.find_element": session.find_element,
            "ui.set_focus": session.set_focus,
            "ui.send_keys_focused": session.send_keys_focused,
            "ui.invoke": session.invoke,
            "ui.drag": session.drag,
            "debug.tracepoint_status": session.tracepoint_status,
        },
    )


def _runner_without_drag(session: ActionSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.find_element": session.find_element,
            "ui.set_focus": session.set_focus,
            "ui.send_keys_focused": session.send_keys_focused,
            "ui.invoke": session.invoke,
            "debug.tracepoint_status": session.tracepoint_status,
        },
    )


def _runner_with_clock(
    session: ActionSmokeSession,
    clock: ManualClock,
) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.find_element": session.find_element,
            "ui.set_focus": session.set_focus,
            "ui.send_keys_focused": session.send_keys_focused,
            "ui.invoke": session.invoke,
            "ui.drag": session.drag,
            "debug.tracepoint_status": session.tracepoint_status,
        },
        clock=clock,
    )


@pytest.mark.asyncio
async def test_v2_ui_key_sequence_focuses_before_sending_keys() -> None:
    session = ActionSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "key sequence route",
            "cases": [
                {
                    "id": "spellcheck_input",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.key_sequence",
                                "selector": {"automation_id": "checkBoxSpellCheckInput"},
                                "keys": "{SPACE}",
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "PASS"
    assert session.calls == [
        ("find_element", {"automation_id": "checkBoxSpellCheckInput"}),
        ("set_focus", {"automation_id": "checkBoxSpellCheckInput"}),
        ("send_keys_focused", "{SPACE}"),
    ]
    assert result["cases"][0]["actions"][0]["route"] == "key_sequence"
    assert result["cases"][0]["actions"][0]["keys"] == "{SPACE}"


@pytest.mark.asyncio
async def test_v2_ui_invoke_route_does_not_focus_before_invoke() -> None:
    session = ActionSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "invoke route",
            "cases": [
                {
                    "id": "invoke_checkbox",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "checkBoxSpellCheckInput"},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "PASS"
    assert session.calls == [
        ("invoke", {"automation_id": "checkBoxSpellCheckInput"}),
    ]
    assert result["cases"][0]["actions"][0]["route"] == "invoke"


@pytest.mark.asyncio
async def test_v2_ui_invoke_invalid_selector_returns_blocked() -> None:
    session = ActionSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "invalid selector",
            "cases": [
                {
                    "id": "invoke_checkbox",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": ["not", "a", "mapping"],
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "BLOCKED"
    assert action["status"] == "BLOCKED"
    assert action["reason"] == "invalid selector payload"
    assert session.calls == []


@pytest.mark.asyncio
async def test_v2_ui_key_sequence_propagates_focus_failure() -> None:
    session = ActionSmokeSession()
    session.focus_result = {"status": "BLOCKED", "reason": "focus backend offline"}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "focus failure",
            "cases": [
                {
                    "id": "spellcheck_input",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.key_sequence",
                                "selector": {"automation_id": "checkBoxSpellCheckInput"},
                                "keys": "{SPACE}",
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "BLOCKED"
    assert action["status"] == "BLOCKED"
    assert action["reason"] == "focus backend offline"
    assert session.calls == [
        ("find_element", {"automation_id": "checkBoxSpellCheckInput"}),
        ("set_focus", {"automation_id": "checkBoxSpellCheckInput"}),
    ]


@pytest.mark.asyncio
async def test_v2_transition_observes_default_idle_settle() -> None:
    session = ActionSmokeSession()
    clock = ManualClock()

    result = await _runner_with_clock(session, clock).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "idle_settle",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "checkBoxSpellCheckInput"},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "PASS"
    assert clock.sleeps_ms == [250]
    assert result["cases"][0]["transitions"][0]["settle"] == {
        "status": "PASS",
        "idle_ms": 250,
    }


@pytest.mark.asyncio
async def test_v2_transition_can_settle_without_action() -> None:
    session = ActionSmokeSession()
    clock = ManualClock()

    result = await _runner_with_clock(session, clock).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "state_only",
                    "transitions": [
                        {
                            "settle": {"idle_ms": 500},
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    transition = result["cases"][0]["transitions"][0]
    assert result["status"] == "PASS"
    assert result["action_count"] == 0
    assert transition["actions"] == []
    assert transition["settle"] == {"status": "PASS", "idle_ms": 500}
    assert clock.sleeps_ms == [500]
    assert session.calls == []


@pytest.mark.asyncio
async def test_v2_wait_and_noop_actions_require_no_selector() -> None:
    session = ActionSmokeSession()
    clock = ManualClock()

    result = await _runner_with_clock(session, clock).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "state_actions",
                    "transitions": [
                        {
                            "action": {"kind": "wait", "idle_ms": 300},
                            "settle": {"idle_ms": 0},
                            "probes": [],
                        },
                        {
                            "action": {"kind": "noop"},
                            "settle": {"idle_ms": 0},
                            "probes": [],
                        },
                    ],
                }
            ],
        }
    )

    actions = result["cases"][0]["actions"]
    assert result["status"] == "PASS"
    assert result["action_count"] == 2
    assert [action["route"] for action in actions] == ["wait", "noop"]
    assert actions[0]["idle_ms"] == 300
    assert clock.sleeps_ms == [300, 0, 0]
    assert session.calls == []


@pytest.mark.parametrize("idle_ms", [True, 1.5])
@pytest.mark.asyncio
async def test_v2_wait_rejects_non_integer_idle_ms(idle_ms: object) -> None:
    session = ActionSmokeSession()
    clock = ManualClock()

    result = await _runner_with_clock(session, clock).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "invalid_wait",
                    "transitions": [
                        {
                            "action": {"kind": "wait", "idle_ms": idle_ms},
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "BLOCKED"
    assert action["status"] == "BLOCKED"
    assert action["reason"] == "invalid wait duration"
    assert action["requested"] == {"idle_ms": idle_ms}
    assert clock.sleeps_ms == []
    assert session.calls == []


@pytest.mark.asyncio
async def test_v2_transition_waits_for_tracepoint_settle() -> None:
    session = ActionSmokeSession()
    session.tracepoint_hits = [False, True]
    clock = ManualClock()

    result = await _runner_with_clock(session, clock).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "tracepoint_settle",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "checkBoxSpellCheckInput"},
                            },
                            "settle": {"await_tracepoint_id": "tp-ready"},
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "PASS"
    assert session.calls == [
        ("invoke", {"automation_id": "checkBoxSpellCheckInput"}),
        ("tracepoint_status", "tp-ready"),
        ("tracepoint_status", "tp-ready"),
    ]
    assert clock.sleeps_ms == [50]
    assert result["cases"][0]["transitions"][0]["settle"] == {
        "status": "PASS",
        "await_tracepoint_id": "tp-ready",
        "tracepoint_timeout_ms": 2000,
    }


@pytest.mark.asyncio
async def test_v2_tracepoint_settle_timeout_returns_blocked() -> None:
    session = ActionSmokeSession()
    clock = ManualClock()

    result = await _runner_with_clock(session, clock).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "tracepoint_timeout",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.invoke",
                                "selector": {"automation_id": "checkBoxSpellCheckInput"},
                            },
                            "settle": {"await_tracepoint_id": "never-hit"},
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "settle condition not met"
    settle = result["cases"][0]["transitions"][0]["settle"]
    assert settle["status"] == "BLOCKED"
    assert settle["reason"] == "settle condition not met"
    assert settle["tracepoint_timeout_ms"] == 2000


@pytest.mark.asyncio
async def test_v2_ui_drag_is_accepted_and_routes_distinct_payloads() -> None:
    session = ActionSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "drag route",
            "cases": [
                {
                    "id": "drag_visible_rows",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {
                                    "selector": {"automation_id": "CueDataGrid"},
                                    "row_index": 1,
                                },
                                "path": [
                                    {"relative_to": "source", "x": 0.5, "y": 0.5},
                                    {"relative_to": "viewport", "x": 0.5, "y": 0.75},
                                ],
                                "drop": {"relative_to": "viewport", "x": 0.5, "y": 0.75},
                            },
                            "probes": [],
                        },
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {
                                    "selector": {"automation_id": "CueDataGrid"},
                                    "row_identity": "Cue 042",
                                },
                                "path": [
                                    {"relative_to": "source", "x": 0.5, "y": 0.5},
                                    {
                                        "relative_to": "viewport",
                                        "x": 0.5,
                                        "y": 0.95,
                                        "hold_ms": 1200,
                                    },
                                ],
                                "drop": {"relative_to": "viewport", "x": 0.5, "y": 0.65},
                                "modifiers": ["ctrl"],
                                "duration_ms": 500,
                            },
                            "probes": [],
                        },
                    ],
                }
            ],
        }
    )

    actions = result["cases"][0]["actions"]
    drag_calls = [call for call in session.calls if call[0] == "drag"]
    assert result["status"] == "PASS"
    assert "ui.drag" in result["accepted_action_kinds"]
    assert [action["route"] for action in actions] == ["drag", "drag"]
    assert len(drag_calls) == 2
    assert drag_calls[0][1]["source"]["row_index"] == 1
    assert drag_calls[1][1]["source"]["row_identity"] == "Cue 042"
    assert drag_calls[0][1]["path"] != drag_calls[1][1]["path"]
    assert (
        actions[0]["route_evidence"]["move_points"]
        != actions[1]["route_evidence"]["move_points"]
    )


@pytest.mark.asyncio
async def test_v2_ui_drag_keeps_route_evidence_compact() -> None:
    session = ActionSmokeSession()
    session.drag_results.append(
        {
            "status": "PASS",
            "backend": "fake",
            "route_evidence": {
                "move_points": [{"relative_to": "screen", "x": 12, "y": 14}],
                "target": {
                    "bounds": {"x": 20, "y": 30, "width": 50, "height": 12},
                    "children": [{"automation_id": "TooLarge"}],
                },
                "window_tree": {"children": [{"automation_id": "Root"}]},
            },
        }
    )

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "compact drag evidence",
            "cases": [
                {
                    "id": "compact_drag_evidence",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {"point": {"x": 10, "y": 10}},
                                "path": [{"relative_to": "screen", "x": 12, "y": 14}],
                                "drop": {"relative_to": "screen", "x": 20, "y": 30},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    route_evidence = result["cases"][0]["actions"][0]["route_evidence"]
    assert result["status"] == "PASS"
    assert route_evidence["move_points"] == [
        {"relative_to": "screen", "x": 12, "y": 14}
    ]
    assert route_evidence["target"]["bounds"] == {
        "x": 20,
        "y": 30,
        "width": 50,
        "height": 12,
    }
    assert "window_tree" not in route_evidence
    assert "children" not in route_evidence["target"]


@pytest.mark.asyncio
async def test_v2_ui_drag_blocks_when_adapter_is_missing() -> None:
    session = ActionSmokeSession()

    result = await _runner_without_drag(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "missing drag adapter",
            "cases": [
                {
                    "id": "missing_drag_adapter",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {"point": {"x": 10, "y": 10}},
                                "path": [{"relative_to": "screen", "x": 12, "y": 14}],
                                "drop": {"relative_to": "screen", "x": 20, "y": 30},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "BLOCKED"
    assert "ui.drag" in result["accepted_action_kinds"]
    assert action["status"] == "BLOCKED"
    assert action["route"] == "drag"
    assert action["reason"] == "service adapter not available"
    assert action["requested"] == {"adapter": "ui.drag"}
    assert "ui.drag" not in action["accepted"]["adapter_names"]
    assert action["next_step"]
    assert session.calls == []


@pytest.mark.parametrize(
    ("action", "reason"),
    [
        (
            {
                "kind": "ui.drag",
                "source": ["not", "an", "object"],
                "path": [{"relative_to": "screen", "x": 10, "y": 10}],
                "drop": {"relative_to": "screen", "x": 20, "y": 20},
            },
            "invalid drag source",
        ),
        (
            {
                "kind": "ui.drag",
                "source": {"point": {"x": 10, "y": 10}},
                "path": [{"relative_to": "screen", "x": 10, "y": 10}],
                "drop": {"relative_to": "screen", "x": 20, "y": 20},
                "modifiers": ["hyper"],
            },
            "invalid drag modifier",
        ),
        (
            {
                "kind": "ui.drag",
                "source": {"point": {"x": 10, "y": 10}},
                "path": [{"relative_to": "screen", "x": 10, "y": 10}],
                "drop": {"relative_to": "screen", "x": 10, "y": 10},
            },
            "zero-distance drag route",
        ),
    ],
)
@pytest.mark.asyncio
async def test_v2_ui_drag_rejects_invalid_payloads(
    action: dict[str, Any],
    reason: str,
) -> None:
    session = ActionSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "invalid drag",
            "cases": [
                {
                    "id": "invalid_drag",
                    "transitions": [{"action": action, "probes": []}],
                }
            ],
        }
    )

    action_result = result["cases"][0]["actions"][0]
    assert result["status"] == "BLOCKED"
    assert action_result["status"] == "BLOCKED"
    assert action_result["reason"] == reason
    assert action_result["requested"]
    assert action_result["accepted"]
    assert action_result["next_step"]
    assert session.calls == []


@pytest.mark.asyncio
async def test_v2_ui_drag_propagates_duplicate_row_identity_blocked() -> None:
    session = ActionSmokeSession()
    session.drag_results.append(
        {
            "status": "BLOCKED",
            "reason": "duplicate row identity",
            "requested": {"row_identity": "Cue 010"},
            "accepted": {"row_identity": "unique visible row identity"},
            "next_step": "Disambiguate the row with row_index or cached_element.",
        }
    )

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "duplicate row identity",
            "cases": [
                {
                    "id": "duplicate_row",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {
                                    "selector": {"automation_id": "CueDataGrid"},
                                    "row_identity": "Cue 010",
                                },
                                "path": [
                                    {"relative_to": "source", "x": 0.5, "y": 0.5},
                                    {"relative_to": "viewport", "x": 0.5, "y": 0.75},
                                ],
                                "drop": {"relative_to": "viewport", "x": 0.5, "y": 0.75},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "BLOCKED"
    assert action["status"] == "BLOCKED"
    assert action["reason"] == "duplicate row identity"
    assert action["requested"] == {"row_identity": "Cue 010"}
    assert action["accepted"] == {"row_identity": "unique visible row identity"}
    assert action["next_step"] == "Disambiguate the row with row_index or cached_element."
