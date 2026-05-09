from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_v2.diff import compute_diff


class DiffSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.calls: list[tuple[str, Any]] = []
        self.debug_values: dict[str, deque[Any]] = defaultdict(deque)
        self.property_values: dict[str, deque[Any]] = defaultdict(deque)

    async def invoke(self, selector: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("ui.invoke", dict(selector)))
        return {"status": "PASS", "invoked": True}

    async def evaluate(self, expression: str) -> dict[str, Any]:
        self.calls.append(("debug.evaluate", expression))
        return {"status": "PASS", "value": self.debug_values[expression].popleft()}

    async def get_property(
        self,
        selector: dict[str, Any],
        property_name: str,
    ) -> dict[str, Any]:
        self.calls.append(("ui.get_property", dict(selector), property_name))
        key = f"{selector['automation_id']}:{property_name}"
        return {"status": "PASS", "value": self.property_values[key].popleft()}


def _runner(session: DiffSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            "debug.evaluate": session.evaluate,
            "ui.get_property": session.get_property,
        },
    )


def _single_case_plan() -> dict[str, Any]:
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "cases": [
            {
                "id": "spellcheck_input",
                "transitions": [
                    {
                        "id": "toggle",
                        "action": {
                            "kind": "ui.invoke",
                            "selector": {"automation_id": "checkBoxSpellCheckInput"},
                        },
                        "probes": [
                            {
                                "kind": "debug.evaluate",
                                "name": "spellcheck_setting",
                                "expression": "_settings.SpellCheckInput",
                                "expected": True,
                            },
                            {
                                "kind": "ui.property",
                                "name": "spellcheck_visible",
                                "selector": {"automation_id": "spellcheckIndicator"},
                                "property": "Visibility",
                                "expected": "Visible",
                            },
                        ],
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_v2_case_records_before_after_and_diff_maps() -> None:
    session = DiffSmokeSession()
    session.debug_values["_settings.SpellCheckInput"].extend([False, True])
    session.property_values["spellcheckIndicator:Visibility"].extend(["Collapsed", "Visible"])

    result = await _runner(session).run(_single_case_plan())

    assert result["status"] == "PASS"
    case = result["cases"][0]
    assert case["status"] == "PASS"
    assert case["before"] == {
        "debug.evaluate.spellcheck_setting": False,
        "ui.property.spellcheck_visible": "Collapsed",
    }
    assert case["after"] == {
        "debug.evaluate.spellcheck_setting": True,
        "ui.property.spellcheck_visible": "Visible",
    }
    assert case["diff"] == {
        "debug.evaluate.spellcheck_setting": {"from": False, "to": True},
        "ui.property.spellcheck_visible": {"from": "Collapsed", "to": "Visible"},
    }
    assert session.calls == [
        ("debug.evaluate", "_settings.SpellCheckInput"),
        ("ui.get_property", {"automation_id": "spellcheckIndicator"}, "Visibility"),
        ("ui.invoke", {"automation_id": "checkBoxSpellCheckInput"}),
        ("debug.evaluate", "_settings.SpellCheckInput"),
        ("ui.get_property", {"automation_id": "spellcheckIndicator"}, "Visibility"),
    ]


@pytest.mark.asyncio
async def test_v2_transition_blocked_payload_comes_from_blocked_probe() -> None:
    session = DiffSmokeSession()
    session.debug_values["_settings.SpellCheckInput"].extend([False, True])
    session.property_values["spellcheckIndicator:Visibility"].extend(
        [
            "Collapsed",
            {
                "status": "BLOCKED",
                "reason": "backend bridge disconnected",
                "requested": {"adapter": "ui.get_property"},
                "accepted": {"adapter_names": ["ui.get_property"]},
                "next_step": "Reconnect UI bridge.",
            },
        ]
    )

    async def get_property(
        selector: dict[str, Any],
        property_name: str,
    ) -> dict[str, Any]:
        session.calls.append(("ui.get_property", dict(selector), property_name))
        value = session.property_values[f"{selector['automation_id']}:{property_name}"].popleft()
        if isinstance(value, dict):
            return value
        return {"status": "PASS", "value": value}

    result = await RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            "debug.evaluate": session.evaluate,
            "ui.get_property": get_property,
        },
    ).run(_single_case_plan())

    transition = result["cases"][0]["transitions"][0]
    assert result["status"] == "BLOCKED"
    assert transition["blocked"]["reason"] == "backend bridge disconnected"
    assert transition["blocked"]["next_step"] == "Reconnect UI bridge."


def test_compute_diff_omits_unchanged_values() -> None:
    assert compute_diff(
        before={
            "debug.evaluate.changed": "Off",
            "ui.property.unchanged": "Visible",
        },
        after={
            "debug.evaluate.changed": "On",
            "ui.property.unchanged": "Visible",
        },
    ) == {
        "debug.evaluate.changed": {"from": "Off", "to": "On"},
    }


def test_compute_diff_includes_removed_values() -> None:
    assert compute_diff(
        before={
            "debug.evaluate.removed": "On",
            "ui.property.added": None,
        },
        after={
            "ui.property.added": "Visible",
        },
    ) == {
        "debug.evaluate.removed": {"from": "On", "to": None},
        "ui.property.added": {"from": None, "to": "Visible"},
    }


@pytest.mark.asyncio
async def test_v2_probe_key_collision_fails_prelaunch() -> None:
    session = DiffSmokeSession()
    plan = _single_case_plan()
    probes = plan["cases"][0]["transitions"][0]["probes"]
    probes.append(dict(probes[0]))

    result = await _runner(session).run(plan)

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert result["validation_errors"] == [
        "cases[0].transitions[0] has duplicate probe path: debug.evaluate.spellcheck_setting"
    ]
    assert session.calls == []
