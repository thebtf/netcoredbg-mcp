from __future__ import annotations

from copy import deepcopy
from typing import Any

from netcoredbg_mcp.session.runtime_smoke_v2.generate import expand_generated_cases


def _plan() -> dict[str, Any]:
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "generate": {
            "template": "toggle-setting-ab",
            "matrix": [
                {
                    "id": "alpha",
                    "control": "checkBoxAlpha",
                    "value": True,
                    "setting_expression": "Settings.Alpha",
                },
                {
                    "id": "beta",
                    "control": "checkBoxBeta",
                    "value": False,
                    "setting_expression": "Settings.Beta",
                },
            ],
        },
    }


def test_matrix_expansion_is_deterministic_across_runs() -> None:
    first, first_errors = expand_generated_cases(deepcopy(_plan()))
    second, second_errors = expand_generated_cases(deepcopy(_plan()))

    assert first_errors == []
    assert second_errors == []
    assert first == second
    assert [case["id"] for case in first] == ["alpha.true", "beta.false"]
    assert [
        transition["action"]["selector"]
        for case in first
        for transition in case["transitions"]
    ] == [{"automation_id": "checkBoxAlpha"}, {"automation_id": "checkBoxBeta"}]
