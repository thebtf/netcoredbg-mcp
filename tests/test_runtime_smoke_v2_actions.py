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
