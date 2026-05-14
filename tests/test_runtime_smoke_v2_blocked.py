from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession


class BlockedSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.calls: list[tuple[str, Any]] = []

    async def find_element(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("find_element", dict(selector)))
        return {"status": "FAIL", "found": False, "reason": "not found"}

    async def set_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("set_focus", dict(selector)))
        return {"status": "PASS"}

    async def send_keys_focused(self, keys: str) -> dict[str, Any]:
        self.calls.append(("send_keys_focused", keys))
        return {"status": "PASS", "sent": keys}


def _runner(session: BlockedSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.find_element": session.find_element,
            "ui.set_focus": session.set_focus,
            "ui.send_keys_focused": session.send_keys_focused,
        },
    )


@pytest.mark.asyncio
async def test_v2_ui_key_sequence_selector_miss_returns_actionable_blocked() -> None:
    session = BlockedSmokeSession()
    selector = {"automation_id": "missingCheckBox"}

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "selector miss",
            "cases": [
                {
                    "id": "missing_control",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.key_sequence",
                                "selector": selector,
                                "keys": "{SPACE}",
                            },
                            "probes": [],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "selector not found"
    assert result["blocked"]["reason"] == "selector not found"
    assert result["blocked"]["requested"]["selector"] == selector
    assert {"automation_id", "name"}.issubset(set(result["blocked"]["accepted"]["selector_keys"]))
    assert result["blocked"]["next_step"]
    assert session.calls == [("find_element", selector)]


@pytest.mark.asyncio
async def test_v2_unknown_probe_kind_fails_prelaunch_with_supported_kinds() -> None:
    session = BlockedSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "unknown probe",
            "cases": [
                {
                    "id": "unsupported_probe",
                    "transitions": [
                        {
                            "action": {
                                "kind": "ui.key_sequence",
                                "selector": {"automation_id": "checkBoxSpellCheckInput"},
                                "keys": "{SPACE}",
                            },
                            "probes": [{"name": "theme", "kind": "ui.colorscheme"}],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "INVALID_SETUP"
    assert result["reason"] == "invalid plan schema"
    assert "ui.colorscheme" in "\n".join(result["validation_errors"])
    assert result["accepted_probe_kinds"] == [
        "debug.evaluate",
        "debug.tracepoint",
        "file.json",
        "output.field",
        "output.since",
        "process.metric",
        "ui.grid",
        "ui.property",
        "ui.text",
    ]
    assert session.calls == []


@pytest.mark.asyncio
async def test_v2_empty_probe_phases_fail_prelaunch() -> None:
    session = BlockedSmokeSession()

    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "name": "empty probe phases",
            "cases": [
                {
                    "id": "empty_probe_phases",
                    "transitions": [
                        {
                            "action": {"kind": "noop"},
                            "probes": [
                                {
                                    "name": "theme",
                                    "kind": "output.field",
                                    "field": "message",
                                    "phases": [],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    assert result["status"] == "INVALID_SETUP"
    assert result["reason"] == "invalid plan schema"
    assert "phases must contain at least one accepted phase name" in "\n".join(
        result["validation_errors"]
    )
    assert session.calls == []
