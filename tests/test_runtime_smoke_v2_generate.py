from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_v2.generate import expand_generated_cases


class GenerateSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.calls: list[tuple[str, Any]] = []

    async def find_element(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("find_element", dict(selector)))
        return {"status": "PASS", "found": True}

    async def set_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("set_focus", dict(selector)))
        return {"status": "PASS"}

    async def send_keys_focused(self, keys: str) -> dict[str, Any]:
        self.calls.append(("send_keys_focused", keys))
        return {"status": "PASS"}

    async def evaluate(self, expression: str) -> dict[str, Any]:
        self.calls.append(("debug.evaluate", expression))
        value = expression.rsplit(".", maxsplit=1)[-1]
        return {"status": "PASS", "value": value == "true"}

    async def get_property(
        self,
        *,
        selector: dict[str, Any],
        property_name: str,
    ) -> dict[str, Any]:
        self.calls.append(("ui.get_property", dict(selector), property_name))
        return {"status": "PASS", "found": True, "value": True}


def _runner(session: GenerateSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.find_element": session.find_element,
            "ui.set_focus": session.set_focus,
            "ui.send_keys_focused": session.send_keys_focused,
            "debug.evaluate": session.evaluate,
            "ui.get_property": session.get_property,
        },
    )


def _generate_plan() -> dict[str, Any]:
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "generate": {
            "template": "toggle-setting-ab",
            "matrix": [
                {
                    "id": "spellcheck_input",
                    "control": "checkBoxSpellCheckInput",
                    "setting_expression": "Settings.SpellCheck.true",
                    "value": True,
                },
                {
                    "id": "line_numbers",
                    "control": "checkBoxLineNumbers",
                    "setting_expression": "Settings.LineNumbers.true",
                    "value": True,
                },
                {
                    "id": "autosave",
                    "control": "checkBoxAutosave",
                    "setting_expression": "Settings.Autosave.true",
                    "value": True,
                },
            ],
        },
        "cases": [],
    }


def test_generate_matrix_expands_in_declaration_order_and_preserves_records() -> None:
    generated, errors = expand_generated_cases(_generate_plan())

    assert errors == []
    assert [case["id"] for case in generated] == [
        "spellcheck_input.true",
        "line_numbers.true",
        "autosave.true",
    ]
    assert [case["rendered_from"] for case in generated] == _generate_plan()["generate"]["matrix"]
    assert [case["transitions"][0]["action"]["selector"] for case in generated] == [
        {"automation_id": "checkBoxSpellCheckInput"},
        {"automation_id": "checkBoxLineNumbers"},
        {"automation_id": "checkBoxAutosave"},
    ]


@pytest.mark.asyncio
async def test_runner_executes_handwritten_cases_before_generated_cases() -> None:
    session = GenerateSmokeSession()
    plan = _generate_plan()
    plan["cases"] = [
        {
            "id": "handwritten_first",
            "transitions": [
                {
                    "action": {
                        "kind": "ui.key_sequence",
                        "selector": {"automation_id": "manualCase"},
                        "keys": "{SPACE}",
                    },
                    "probes": [],
                }
            ],
        }
    ]

    result = await _runner(session).run(plan)

    assert result["status"] == "PASS"
    assert result["generated_case_count"] == 3
    assert [case["id"] for case in result["cases"]] == [
        "handwritten_first",
        "spellcheck_input.true",
        "line_numbers.true",
        "autosave.true",
    ]
    assert result["cases"][1]["rendered_from"] == plan["generate"]["matrix"][0]


@pytest.mark.asyncio
async def test_duplicate_generated_case_id_fails_before_launch() -> None:
    session = GenerateSmokeSession()
    plan = _generate_plan()
    plan["generate"]["matrix"] = [
        {
            "id": "spellcheck_input",
            "control": "checkBoxSpellCheckInput",
            "value": True,
        },
        {
            "id": "spellcheck_input",
            "control": "checkBoxSpellCheckInput",
            "value": True,
        },
    ]

    result = await _runner(session).run(plan)

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert "duplicate case id: spellcheck_input.true" in result["validation_errors"]
    assert session.calls == []
