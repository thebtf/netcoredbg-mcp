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
        self.find_result: dict[str, Any] = {"status": "PASS", "found": True}
        self.text_read_result: dict[str, Any] = {"status": "PASS", "text": ""}
        self.text_get_state_result: dict[str, Any] = {
            "status": "PASS",
            "text": "Original text",
            "selection": {"start": 0, "end": 13, "length": 13},
            "selectionStart": 0,
            "selectionLength": 13,
        }
        self.drag_results: list[dict[str, Any]] = []
        self.grid_select_indices_results: list[dict[str, Any]] = []

    async def find_element(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("find_element", dict(selector)))
        return {**self.find_result, "selector": dict(selector)}

    async def set_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("set_focus", dict(selector)))
        return dict(self.focus_result)

    async def send_keys_focused(self, keys: str) -> dict[str, Any]:
        self.calls.append(("send_keys_focused", keys))
        return {**self.send_keys_result, "sent": keys}

    async def text_read(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("text_read", dict(selector)))
        return dict(self.text_read_result)

    async def text_get_state(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("text_get_state", dict(selector)))
        return dict(self.text_get_state_result)

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

    async def grid_select_indices(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("grid_select_indices", request))
        if self.grid_select_indices_results:
            return self.grid_select_indices_results.pop(0)
        return {
            "status": "PASS",
            "selected_indices": list(request.get("indices") or []),
            "selected_count": len(request.get("indices") or []),
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
            "ui.text.read": session.text_read,
            "ui.text.get_state": session.text_get_state,
            "ui.invoke": session.invoke,
            "ui.drag": session.drag,
            "ui.grid.select_indices": session.grid_select_indices,
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
            "ui.text.read": session.text_read,
            "ui.text.get_state": session.text_get_state,
            "ui.invoke": session.invoke,
            "debug.tracepoint_status": session.tracepoint_status,
        },
    )


def _runner_without_text_state(session: ActionSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.find_element": session.find_element,
            "ui.set_focus": session.set_focus,
            "ui.send_keys_focused": session.send_keys_focused,
            "ui.text.read": session.text_read,
            "ui.invoke": session.invoke,
            "ui.drag": session.drag,
            "ui.grid.select_indices": session.grid_select_indices,
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
            "ui.text.read": session.text_read,
            "ui.text.get_state": session.text_get_state,
            "ui.invoke": session.invoke,
            "ui.drag": session.drag,
            "ui.grid.select_indices": session.grid_select_indices,
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
async def test_v2_ui_text_type_replace_selection_focuses_types_and_verifies() -> None:
    session = ActionSmokeSession()
    session.text_read_result = {"status": "PASS", "text": "Replaced text"}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "replace textbox text",
            "cases": [
                {
                    "id": "replace_text",
                    "transitions": [
                        {
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

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "PASS"
    assert "ui.text.type_replace_selection" in result["accepted_action_kinds"]
    assert session.calls == [
        ("find_element", {"automation_id": "CueTextBox"}),
        ("set_focus", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "^a"),
        ("text_get_state", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "Replaced text"),
        ("text_read", {"automation_id": "CueTextBox"}),
    ]
    assert action["route"] == "text_type_replace_selection"
    assert action["verified"] is True
    assert action["text"] == "Replaced text"
    assert action["precondition"]["selected"] is True
    assert action["precondition"]["expected"] == {
        "selection_start": 0,
        "selection_end": 13,
        "selection_length": 13,
        "text_length": 13,
    }


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_accepts_value_field_from_text_read() -> None:
    session = ActionSmokeSession()
    session.text_read_result = {"status": "PASS", "value": "Replaced text"}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "replace_text_value_fallback",
                    "transitions": [
                        {
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

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "PASS"
    assert action["verified"] is True
    assert action["text"] == "Replaced text"


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_accepts_statusless_backend_success() -> None:
    session = ActionSmokeSession()
    session.find_result = {"found": True, "automationId": "CueTextBox"}
    session.focus_result = {"focused": True, "method": "UIA.Focus"}
    session.send_keys_result = {"sent": True}
    session.text_read_result = {"value": "Replaced text", "source": "ValuePattern"}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "replace_text_statusless_success",
                    "transitions": [
                        {
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

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "PASS"
    assert action["verified"] is True
    assert action["result"]["source"] == "ValuePattern"
    assert session.calls == [
        ("find_element", {"automation_id": "CueTextBox"}),
        ("set_focus", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "^a"),
        ("text_get_state", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "Replaced text"),
        ("text_read", {"automation_id": "CueTextBox"}),
    ]


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_blocks_on_bad_select_all() -> None:
    session = ActionSmokeSession()
    session.text_get_state_result = {
        "status": "PASS",
        "text": "Original text",
        "selection": {"start": 0, "end": 0, "length": 0},
        "selectionStart": 0,
        "selectionLength": 0,
    }

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "replace_text_precondition_failed",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.text.type_replace_selection",
                                "selector": {"automation_id": "CueTextBox"},
                                "text": "Must not type",
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
    assert action["reason"] == "select-all precondition failed"
    assert action["precondition"]["selected"] is False
    assert action["precondition"]["expected"] == {
        "selection_start": 0,
        "selection_end": 13,
        "selection_length": 13,
        "text_length": 13,
    }
    assert action["precondition"]["actual"] == {
        "selection_start": 0,
        "selection_end": 0,
        "selection_length": 0,
        "text_length": 13,
    }
    assert session.calls == [
        ("find_element", {"automation_id": "CueTextBox"}),
        ("set_focus", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "^a"),
        ("text_get_state", {"automation_id": "CueTextBox"}),
    ]


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_blocks_without_text_state() -> None:
    session = ActionSmokeSession()
    session.text_get_state_result = {
        "status": "PASS",
        "selection": {"start": 0, "end": 0, "length": 0},
    }

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "replace_text_missing_text_state",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.text.type_replace_selection",
                                "selector": {"automation_id": "CueTextBox"},
                                "text": "Must not type",
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
    assert action["reason"] == "select-all precondition failed"
    assert action["precondition"]["selected"] is False
    assert action["precondition"]["reason"] == "TextBox text evidence unavailable"
    assert action["precondition"]["expected"] == {
        "text": "bounded TextBox text or value evidence",
    }
    assert action["precondition"]["actual"] == {
        "selection_start": 0,
        "selection_end": 0,
        "selection_length": 0,
    }
    assert session.calls == [
        ("find_element", {"automation_id": "CueTextBox"}),
        ("set_focus", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "^a"),
        ("text_get_state", {"automation_id": "CueTextBox"}),
    ]


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_accepts_float_string_selection_offsets() -> None:
    session = ActionSmokeSession()
    session.text_get_state_result = {
        "status": "PASS",
        "text": "Original text",
        "selection": {"start": "0.0", "end": "13.0", "length": "13.0"},
    }
    session.text_read_result = {"status": "PASS", "text": "Replaced text"}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "replace_text_float_offsets",
                    "transitions": [
                        {
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

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "PASS"
    assert action["precondition"]["selected"] is True
    assert action["precondition"]["actual"] == {
        "selection_start": 0,
        "selection_end": 13,
        "selection_length": 13,
        "text_length": 13,
    }
    assert ("send_keys_focused", "Replaced text") in session.calls


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_infers_start_from_end_and_length() -> None:
    session = ActionSmokeSession()
    session.text_get_state_result = {
        "status": "PASS",
        "text": "Original text",
        "selection": {"end": 13, "length": 13},
    }
    session.text_read_result = {"status": "PASS", "text": "Replaced text"}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "replace_text_inferred_start",
                    "transitions": [
                        {
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

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "PASS"
    assert action["precondition"]["selected"] is True
    assert action["precondition"]["actual"] == {
        "selection_start": 0,
        "selection_end": 13,
        "selection_length": 13,
        "text_length": 13,
    }
    assert ("send_keys_focused", "Replaced text") in session.calls


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_blocks_without_selection_state() -> None:
    session = ActionSmokeSession()
    session.text_get_state_result = {
        "status": "PASS",
        "text": "Original text",
    }

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "replace_text_missing_selection_state",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.text.type_replace_selection",
                                "selector": {"automation_id": "CueTextBox"},
                                "text": "Must not type",
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
    assert action["precondition"]["selected"] is False
    assert action["precondition"]["reason"] == "TextBox selection evidence unavailable"
    assert action["precondition"]["state"] == {
        "status": "PASS",
        "text": "Original text",
    }
    assert session.calls == [
        ("find_element", {"automation_id": "CueTextBox"}),
        ("set_focus", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "^a"),
        ("text_get_state", {"automation_id": "CueTextBox"}),
    ]


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_blocks_without_state_adapter() -> None:
    session = ActionSmokeSession()

    result = await _runner_without_text_state(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "replace_text_no_state_adapter",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.text.type_replace_selection",
                                "selector": {"automation_id": "CueTextBox"},
                                "text": "Must not type",
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
    assert action["reason"] == "service adapter not available"
    assert action["selection_keys"] == "^a"
    assert session.calls == [
        ("find_element", {"automation_id": "CueTextBox"}),
        ("set_focus", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "^a"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_status", "expected_status"),
    [
        ("ERROR", "FAIL"),
        ("UNSUPPORTED", "BLOCKED"),
        ("INVALID_SETUP", "BLOCKED"),
    ],
)
async def test_v2_ui_text_type_replace_selection_normalizes_adapter_failure_statuses(
    adapter_status: str,
    expected_status: str,
) -> None:
    session = ActionSmokeSession()
    session.focus_result = {
        "status": adapter_status,
        "reason": "focus backend unavailable",
    }

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": f"replace_text_{adapter_status.lower()}",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.text.type_replace_selection",
                                "selector": {"automation_id": "CueTextBox"},
                                "text": "Never typed",
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == expected_status
    assert action["status"] == expected_status
    assert action["result"]["status"] == adapter_status
    assert action["reason"] == "focus backend unavailable"
    assert session.calls == [
        ("find_element", {"automation_id": "CueTextBox"}),
        ("set_focus", {"automation_id": "CueTextBox"}),
    ]


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_blocks_selector_miss_before_typing() -> None:
    session = ActionSmokeSession()
    session.find_result = {"status": "PASS", "found": False}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "missing_textbox",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.text.type_replace_selection",
                                "selector": {"automation_id": "MissingCueTextBox"},
                                "text": "Never typed",
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
    assert action["reason"] == "selector not found"
    assert session.calls == [("find_element", {"automation_id": "MissingCueTextBox"})]


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_blocks_missing_selector_before_lookup() -> None:
    session = ActionSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "missing_selector",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.text.type_replace_selection",
                                "text": "Never typed",
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
    assert action["reason"] == "missing selector payload"
    assert session.calls == []


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_fails_on_post_read_mismatch() -> None:
    session = ActionSmokeSession()
    session.text_read_result = {"status": "PASS", "text": "Old text"}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "replace_mismatch",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.text.type_replace_selection",
                                "selector": {"automation_id": "CueTextBox"},
                                "text": "Expected text",
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "FAIL"
    assert action["status"] == "FAIL"
    assert action["reason"] == "post-read text mismatch"
    assert action["expected"] == "Expected text"
    assert action["actual"] == "Old text"
    assert session.calls == [
        ("find_element", {"automation_id": "CueTextBox"}),
        ("set_focus", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "^a"),
        ("text_get_state", {"automation_id": "CueTextBox"}),
        ("send_keys_focused", "Expected text"),
        ("text_read", {"automation_id": "CueTextBox"}),
    ]


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_escapes_sendkeys_literals() -> None:
    session = ActionSmokeSession()
    session.text_read_result = {"status": "PASS", "text": "A+^%{}()~"}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "replace_special_text",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.text.type_replace_selection",
                                "selector": {"automation_id": "CueTextBox"},
                                "text": "A+^%{}()~",
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "PASS"
    assert ("send_keys_focused", "^a") in session.calls
    assert ("send_keys_focused", "A{+}{^}{%}{{}{}}{(}{)}{~}") in session.calls
    assert action["keys"] == "^aA{+}{^}{%}{{}{}}{(}{)}{~}"
    assert action["selection_keys"] == "^a"
    assert action["input_keys"] == "A{+}{^}{%}{{}{}}{(}{)}{~}"


@pytest.mark.asyncio
async def test_v2_ui_text_type_replace_selection_clears_text_when_replacement_is_empty() -> None:
    session = ActionSmokeSession()
    session.text_read_result = {"status": "PASS", "text": ""}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "cases": [
                {
                    "id": "clear_textbox",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.text.type_replace_selection",
                                "selector": {"automation_id": "CueTextBox"},
                                "text": "",
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "PASS"
    assert ("send_keys_focused", "^a") in session.calls
    assert ("send_keys_focused", "{BACKSPACE}") in session.calls
    assert action["keys"] == "^a{BACKSPACE}"
    assert action["selection_keys"] == "^a"
    assert action["input_keys"] == "{BACKSPACE}"
    assert action["text"] == ""


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
                        {
                            "action": {"kind": "ui.noop"},
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
    assert result["action_count"] == 3
    assert [action["route"] for action in actions] == ["wait", "noop", "noop"]
    assert actions[0]["idle_ms"] == 300
    assert "noop" in result["accepted_action_kinds"]
    assert "ui.noop" in result["accepted_action_kinds"]
    assert clock.sleeps_ms == [300, 0, 0, 0]
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
async def test_v2_ui_drag_distinguishes_source_forms_in_request_and_evidence() -> None:
    session = ActionSmokeSession()
    source_cases = [
        (
            "row_index_source",
            {
                "selector": {"automation_id": "CueDataGrid"},
                "row_index": 2,
            },
            "row_index",
        ),
        (
            "row_identity_source",
            {
                "selector": {"automation_id": "CueDataGrid"},
                "row_identity": "Cue 042",
            },
            "row_identity",
        ),
        (
            "selector_source",
            {"selector": {"automation_id": "CueDragHandle"}},
            "selector",
        ),
        (
            "point_source",
            {"point": {"relative_to": "screen", "x": 25, "y": 40}},
            "point",
        ),
    ]

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "source resolution",
            "cases": [
                {
                    "id": "source_resolution",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": source,
                                "path": [
                                    {"relative_to": "source", "x": 0.5, "y": 0.5},
                                    {
                                        "relative_to": "viewport",
                                        "x": 0.5,
                                        "y": 0.55 + (index * 0.1),
                                    },
                                ],
                                "drop": {
                                    "relative_to": "viewport",
                                    "x": 0.5,
                                    "y": 0.55 + (index * 0.1),
                                },
                            },
                            "probes": [],
                        }
                        for index, (_case_id, source, _kind) in enumerate(source_cases)
                    ],
                }
            ],
        }
    )

    actions = result["cases"][0]["actions"]
    drag_calls = [call for call in session.calls if call[0] == "drag"]
    assert result["status"] == "PASS"
    assert [call[1]["source"]["kind"] for call in drag_calls] == [
        kind for (_case_id, _source, kind) in source_cases
    ]
    assert [action["route_evidence"]["source"]["kind"] for action in actions] == [
        kind for (_case_id, _source, kind) in source_cases
    ]
    assert drag_calls[0][1]["source"]["row_index"] == 2
    assert drag_calls[1][1]["source"]["row_identity"] == "Cue 042"
    assert drag_calls[2][1]["source"]["selector"] == {"automation_id": "CueDragHandle"}
    assert drag_calls[3][1]["source"]["point"] == {
        "relative_to": "screen",
        "x": 25,
        "y": 40,
    }


@pytest.mark.asyncio
async def test_v2_ui_drag_rejects_fractional_row_index() -> None:
    session = ActionSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "fractional drag row index",
            "cases": [
                {
                    "id": "fractional_drag_row_index",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {
                                    "selector": {"automation_id": "CueDataGrid"},
                                    "row_index": 1.5,
                                },
                                "path": [{"relative_to": "source", "x": 0.5, "y": 0.5}],
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
    assert action["reason"] == "invalid drag source"
    assert action["requested"] == {"row_index": 1.5}
    assert session.calls == []


@pytest.mark.asyncio
async def test_v2_ui_grid_select_routes_non_contiguous_indices() -> None:
    session = ActionSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "grid multi-select",
            "cases": [
                {
                    "id": "grid_multi_select",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.grid.select",
                                "selector": {"automation_id": "CueDataGrid"},
                                "indices": [1, 4],
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    select_call = next(call for call in session.calls if call[0] == "grid_select_indices")
    assert result["status"] == "PASS"
    assert "ui.grid.select" in result["accepted_action_kinds"]
    assert select_call[1]["selector"] == {"automation_id": "CueDataGrid"}
    assert select_call[1]["indices"] == [1, 4]
    assert action["route"] == "grid_select"
    assert action["indices"] == [1, 4]


@pytest.mark.asyncio
async def test_v2_ui_grid_select_propagates_backend_blocked() -> None:
    session = ActionSmokeSession()
    session.grid_select_indices_results.append(
        {
            "status": "BLOCKED",
            "reason": "multi-select backend did not select all requested rows",
            "requested": {"adapter": "ui.grid.select_indices"},
            "accepted": {"backend": "FlaUI multi_select"},
            "next_step": "Run with a backend that can perform real multi-select.",
        }
    )

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "grid multi-select blocked",
            "cases": [
                {
                    "id": "grid_multi_select_blocked",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.grid.select",
                                "selector": {"automation_id": "CueDataGrid"},
                                "indices": [1, 4],
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
    assert action["reason"] == "multi-select backend did not select all requested rows"
    assert action["next_step"]


@pytest.mark.asyncio
async def test_v2_ui_grid_select_rejects_fractional_indices() -> None:
    session = ActionSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "grid multi-select invalid index",
            "cases": [
                {
                    "id": "grid_multi_select_invalid_index",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.grid.select",
                                "selector": {"automation_id": "CueDataGrid"},
                                "indices": [1.5],
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
    assert action["reason"] == "invalid grid selection index"
    assert action["requested"] == {"index": 1.5}
    assert session.calls == []


@pytest.mark.parametrize(
    ("selection_mode", "selected_identities"),
    [
        ("contiguous", ["Cue 001", "Cue 002"]),
        ("non_contiguous", ["Cue 001", "Cue 004"]),
    ],
)
@pytest.mark.asyncio
async def test_v2_ui_drag_preserves_selected_payload_evidence(
    selection_mode: str,
    selected_identities: list[str],
) -> None:
    session = ActionSmokeSession()
    session.drag_results.append(
        {
            "status": "PASS",
            "backend": "fake",
            "route_evidence": {
                "move_points": [{"relative_to": "viewport", "x": 0.5, "y": 0.75}],
            },
            "selected_payload": {
                "before": selected_identities,
                "after": selected_identities,
                "selection_mode": selection_mode,
                "preserved": True,
            },
        }
    )

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": f"{selection_mode} selected payload drag",
            "cases": [
                {
                    "id": f"{selection_mode}_selected_payload",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {
                                    "selector": {"automation_id": "CueDataGrid"},
                                    "row_identity": selected_identities[0],
                                },
                                "path": [
                                    {"relative_to": "source", "x": 0.5, "y": 0.5},
                                    {"relative_to": "viewport", "x": 0.5, "y": 0.75},
                                ],
                                "drop": {"relative_to": "viewport", "x": 0.5, "y": 0.75},
                                "identity": {"column": "StableRowId"},
                                "expect": {
                                    "selected_payload_preserved": True,
                                    "selected_payload": {
                                        "expected_identities": selected_identities,
                                        "selection_mode": selection_mode,
                                    },
                                },
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    drag_call = next(call for call in session.calls if call[0] == "drag")
    assert result["status"] == "PASS"
    assert drag_call[1]["identity"] == {"column": "StableRowId"}
    assert drag_call[1]["expect"]["selected_payload"]["expected_identities"] == selected_identities
    assert action["selected_payload"] == {
        "before": selected_identities,
        "after": selected_identities,
        "selection_mode": selection_mode,
        "preserved": True,
    }


@pytest.mark.parametrize(
    ("adapter_result", "expected_status", "expected_reason"),
    [
        (
            {
                "status": "PASS",
                "backend": "fake",
                "selected_payload": {
                    "before": ["Cue 001", "Cue 002"],
                    "after": ["Cue 001", "Cue 003"],
                },
            },
            "FAIL",
            "selected payload expectation failed",
        ),
        (
            {
                "status": "PASS",
                "backend": "fake",
                "selected_payload": {
                    "before": [],
                    "after": [],
                },
            },
            "FAIL",
            "selected payload expectation failed",
        ),
        (
            {
                "status": "PASS",
                "backend": "fake",
                "route_evidence": {"move_points": []},
            },
            "BLOCKED",
            "selected payload evidence unavailable",
        ),
    ],
)
@pytest.mark.asyncio
async def test_v2_ui_drag_fails_closed_for_selected_payload_expectation(
    adapter_result: dict[str, Any],
    expected_status: str,
    expected_reason: str,
) -> None:
    session = ActionSmokeSession()
    session.drag_results.append(adapter_result)

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "selected payload fail closed",
            "cases": [
                {
                    "id": "selected_payload_fail_closed",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {
                                    "selector": {"automation_id": "CueDataGrid"},
                                    "row_identity": "Cue 001",
                                },
                                "path": [
                                    {"relative_to": "source", "x": 0.5, "y": 0.5},
                                    {"relative_to": "viewport", "x": 0.5, "y": 0.75},
                                ],
                                "drop": {"relative_to": "viewport", "x": 0.5, "y": 0.75},
                                "expect": {"selected_payload_preserved": True},
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == expected_status
    assert action["status"] == expected_status
    assert action["reason"] == expected_reason


@pytest.mark.asyncio
async def test_v2_ui_drag_reports_no_op_and_cleanup_evidence() -> None:
    session = ActionSmokeSession()
    session.drag_results.append(
        {
            "status": "PASS",
            "backend": "fake",
            "route_evidence": {
                "move_points": [
                    {"relative_to": "source", "x": 0.5, "y": 0.5},
                    {"relative_to": "source", "x": 0.52, "y": 0.5},
                ],
                "final_pointer": {"x": 102, "y": 100},
            },
            "no_op": {
                "expected": True,
                "reason": "small_movement",
                "route_attempted": True,
                "movement_px": 2,
            },
            "cleanup": {
                "modifier_cleanup": {"released": ["SHIFT"]},
                "pointer_cleanup": {"left_button_released": True},
            },
        }
    )

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "small movement no-op",
            "cases": [
                {
                    "id": "small_movement_noop",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {
                                    "selector": {"automation_id": "CueDataGrid"},
                                    "row_identity": "Cue 001",
                                },
                                "path": [
                                    {"relative_to": "source", "x": 0.5, "y": 0.5},
                                    {"relative_to": "source", "x": 0.52, "y": 0.5},
                                ],
                                "drop": {"relative_to": "source", "x": 0.52, "y": 0.5},
                                "modifiers": ["shift"],
                                "expect": {
                                    "no_op": True,
                                    "no_op_reason": "small_movement",
                                },
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "PASS"
    assert action["no_op"] == {
        "expected": True,
        "reason": "small_movement",
        "route_attempted": True,
        "movement_px": 2,
    }
    assert action["cleanup"] == {
        "modifier_cleanup": {"released": ["SHIFT"]},
        "pointer_cleanup": {"left_button_released": True},
    }


@pytest.mark.parametrize("reason", ["cancelled", "invalid_drop"])
@pytest.mark.asyncio
async def test_v2_ui_drag_accepts_negative_no_op_reason(reason: str) -> None:
    session = ActionSmokeSession()
    session.drag_results.append(
        {
            "status": "PASS",
            "backend": "fake",
            "route_evidence": {
                "move_points": [{"relative_to": "source", "x": 0.6, "y": 0.5}],
                "final_pointer": {"relative_to": "source", "x": 0.6, "y": 0.5},
            },
            "no_op": {
                "expected": True,
                "reason": reason,
                "route_attempted": True,
            },
            "cleanup": {
                "modifier_cleanup": {"released": []},
                "pointer_cleanup": {"left_button_released": True},
            },
        }
    )

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": f"{reason} no-op",
            "cases": [
                {
                    "id": f"{reason}_noop",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {
                                    "selector": {"automation_id": "CueDataGrid"},
                                    "row_identity": "Cue 001",
                                },
                                "path": [
                                    {"relative_to": "source", "x": 0.5, "y": 0.5},
                                    {"relative_to": "source", "x": 0.6, "y": 0.5},
                                ],
                                "drop": {"relative_to": "source", "x": 0.6, "y": 0.5},
                                "expect": {
                                    "no_op": True,
                                    "no_op_reason": reason,
                                },
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == "PASS"
    assert action["no_op"]["reason"] == reason
    assert action["cleanup"]["pointer_cleanup"]["left_button_released"] is True


@pytest.mark.asyncio
async def test_v2_ui_drag_forwards_cancel_request_to_adapter() -> None:
    session = ActionSmokeSession()
    session.drag_results.append(
        {
            "status": "PASS",
            "backend": "fake",
            "route_evidence": {
                "move_points": [{"relative_to": "drop", "x": 0.5, "y": 0.5}],
                "final_pointer": {"relative_to": "drop", "x": 0.5, "y": 0.5},
            },
            "no_op": {"expected": True, "reason": "cancelled"},
            "cleanup": {
                "modifier_cleanup": {"released": []},
                "pointer_cleanup": {"left_button_released": True},
            },
        }
    )

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "cancelled drag",
            "cases": [
                {
                    "id": "cancelled_drag",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {
                                    "selector": {"automation_id": "CueDataGrid"},
                                    "row_identity": "Cue 001",
                                },
                                "path": [
                                    {"relative_to": "source", "x": 0.5, "y": 0.5},
                                    {"relative_to": "drop", "x": 0.5, "y": 0.5},
                                ],
                                "drop": {
                                    "selector": {"automation_id": "CueDataGrid"},
                                    "row_index": 2,
                                },
                                "cancel": {"key": "escape"},
                                "expect": {
                                    "no_op": True,
                                    "no_op_reason": "cancelled",
                                },
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    drag_call = next(call for call in session.calls if call[0] == "drag")
    action = result["cases"][0]["actions"][0]
    assert result["status"] == "PASS"
    assert drag_call[1]["cancel"] == {"key": "escape"}
    assert action["cancel"] == {"key": "escape"}


@pytest.mark.parametrize(
    ("adapter_result", "expected_status", "expected_reason"),
    [
        (
            {
                "status": "PASS",
                "backend": "fake",
                "route_evidence": {
                    "move_points": [{"relative_to": "source", "x": 0.6, "y": 0.5}],
                    "final_pointer": {"relative_to": "source", "x": 0.6, "y": 0.5},
                },
            },
            "BLOCKED",
            "no-op evidence unavailable",
        ),
        (
            {
                "status": "PASS",
                "backend": "fake",
                "route_evidence": {
                    "move_points": [{"relative_to": "source", "x": 0.6, "y": 0.5}],
                    "final_pointer": {"relative_to": "source", "x": 0.6, "y": 0.5},
                },
                "no_op": {"expected": True, "reason": "cancelled"},
            },
            "BLOCKED",
            "cleanup evidence unavailable",
        ),
        (
            {
                "status": "PASS",
                "backend": "fake",
                "route_evidence": {
                    "move_points": [{"relative_to": "source", "x": 0.6, "y": 0.5}],
                    "final_pointer": {"relative_to": "source", "x": 0.6, "y": 0.5},
                },
                "no_op": {"expected": False, "reason": "invalid_drop"},
                "cleanup": {
                    "modifier_cleanup": {"released": []},
                    "pointer_cleanup": {"left_button_released": True},
                },
            },
            "FAIL",
            "no-op expectation failed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_v2_ui_drag_fails_closed_for_no_op_expectation(
    adapter_result: dict[str, Any],
    expected_status: str,
    expected_reason: str,
) -> None:
    session = ActionSmokeSession()
    session.drag_results.append(adapter_result)

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "negative drag fail closed",
            "cases": [
                {
                    "id": "negative_drag_fail_closed",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.drag",
                                "source": {
                                    "selector": {"automation_id": "CueDataGrid"},
                                    "row_identity": "Cue 001",
                                },
                                "path": [
                                    {"relative_to": "source", "x": 0.5, "y": 0.5},
                                    {"relative_to": "source", "x": 0.6, "y": 0.5},
                                ],
                                "drop": {"relative_to": "source", "x": 0.6, "y": 0.5},
                                "expect": {
                                    "no_op": True,
                                    "no_op_reason": "invalid_drop",
                                },
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    action = result["cases"][0]["actions"][0]
    assert result["status"] == expected_status
    assert action["status"] == expected_status
    assert action["reason"] == expected_reason


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


@pytest.mark.asyncio
async def test_v2_ui_drag_blocks_pass_without_route_evidence() -> None:
    session = ActionSmokeSession()
    session.drag_results.append(
        {
            "status": "PASS",
            "backend": "diagnostic-shortcut",
        }
    )

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "drag false pass guard",
            "cases": [
                {
                    "id": "drag_false_pass_guard",
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
    assert action["status"] == "BLOCKED"
    assert action["reason"] == "real pointer route evidence unavailable"
    assert action["requested"] == {
        "adapter_status": "PASS",
        "route_evidence": None,
    }
    assert action["accepted"]["route_evidence"]
    assert action["next_step"]


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
        (
            {
                "kind": "ui.drag",
                "source": {
                    "selector": {"automation_id": "CueDataGrid"},
                    "row_index": 1,
                    "point": {"relative_to": "screen", "x": 10, "y": 10},
                },
                "path": [{"relative_to": "screen", "x": 10, "y": 10}],
                "drop": {"relative_to": "screen", "x": 20, "y": 20},
            },
            "ambiguous drag source",
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
                                "expect": {
                                    "selected_payload_preserved": True,
                                    "no_op": True,
                                },
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
