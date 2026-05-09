from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from netcoredbg_mcp.session.runtime_smoke_v2.generate import expand_generated_cases

FIXTURE_ROOT = Path(__file__).parent / "fixtures"


def test_v2_radio_group_set_renders_wpf_mode_triple() -> None:
    assert (FIXTURE_ROOT / "WpfSmokeApp" / "WpfSmokeApp.csproj").exists()

    generated, errors = expand_generated_cases({
        "generate": {
            "template": "radio-group-set",
            "matrix": [
                {
                    "id": "mode_generic",
                    "value": "generic",
                    "expression": "ViewModel.Mode",
                    "controls": [
                        {"value": "generic", "automation_id": "checkBoxGeneric"},
                        {"value": "scanning", "automation_id": "checkBoxScanning"},
                        {"value": "transcribe", "automation_id": "checkBoxTranscribe"},
                    ],
                },
                {
                    "id": "mode_scanning",
                    "value": "scanning",
                    "expression": "ViewModel.Mode",
                    "controls": [
                        {"value": "generic", "automation_id": "checkBoxGeneric"},
                        {"value": "scanning", "automation_id": "checkBoxScanning"},
                        {"value": "transcribe", "automation_id": "checkBoxTranscribe"},
                    ],
                },
                {
                    "id": "mode_transcribe",
                    "value": "transcribe",
                    "expression": "ViewModel.Mode",
                    "controls": [
                        {"value": "generic", "automation_id": "checkBoxGeneric"},
                        {"value": "scanning", "automation_id": "checkBoxScanning"},
                        {"value": "transcribe", "automation_id": "checkBoxTranscribe"},
                    ],
                },
            ],
        }
    })

    assert errors == []
    assert len(generated) == 3
    assert [case["id"] for case in generated] == [
        "mode_generic.generic",
        "mode_scanning.scanning",
        "mode_transcribe.transcribe",
    ]


def test_v2_avalonia_matrix_toggle_is_deterministic() -> None:
    assert (FIXTURE_ROOT / "AvaloniaSmokeApp" / "AvaloniaSmokeApp.csproj").exists()
    plan = {
        "generate": {
            "template": "toggle-setting-ab",
            "matrix": [
                {"id": "avalonia_alpha", "control": "alphaToggle", "value": True},
                {"id": "avalonia_beta", "control": "betaToggle", "value": False},
                {"id": "avalonia_gamma", "control": "gammaToggle", "value": True},
            ],
        }
    }

    first, first_errors = expand_generated_cases(deepcopy(plan))
    second, second_errors = expand_generated_cases(deepcopy(plan))

    assert first_errors == []
    assert second_errors == []
    assert first == second
    assert len(first) == 3
