from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession


class ActionSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.calls: list[tuple[str, Any]] = []

    async def find_element(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("find_element", dict(selector)))
        return {"status": "PASS", "found": True, "selector": dict(selector)}

    async def set_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("set_focus", dict(selector)))
        return {"status": "PASS"}

    async def send_keys_focused(self, keys: str) -> dict[str, Any]:
        self.calls.append(("send_keys_focused", keys))
        return {"status": "PASS", "sent": keys}

    async def invoke(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("invoke", dict(selector)))
        return {"status": "PASS", "method": "InvokePattern"}


def _runner(session: ActionSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.find_element": session.find_element,
            "ui.set_focus": session.set_focus,
            "ui.send_keys_focused": session.send_keys_focused,
            "ui.invoke": session.invoke,
        },
    )


@pytest.mark.asyncio
async def test_v2_ui_key_sequence_focuses_before_sending_keys() -> None:
    session = ActionSmokeSession()

    result = await _runner(session).run({
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
    })

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

    result = await _runner(session).run({
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
    })

    assert result["status"] == "PASS"
    assert session.calls == [
        ("invoke", {"automation_id": "checkBoxSpellCheckInput"}),
    ]
    assert result["cases"][0]["actions"][0]["route"] == "invoke"
