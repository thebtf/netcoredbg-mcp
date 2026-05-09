from __future__ import annotations

import pytest

from netcoredbg_mcp.session.runtime_smoke_v2.generate import expand_generated_cases
from netcoredbg_mcp.session.runtime_smoke_v2.templates import accepted_template_names
from netcoredbg_mcp.session.runtime_smoke_v2.templates._substituter import (
    TemplateRenderError,
    render_template_value,
)


def test_template_registry_exposes_three_builtin_templates() -> None:
    assert accepted_template_names() == [
        "radio-group-set",
        "setting-ab-row-effect",
        "toggle-setting-ab",
    ]


def test_toggle_setting_template_generates_different_keyboard_cases() -> None:
    generated, errors = expand_generated_cases(
        {
            "generate": {
                "template": "toggle-setting-ab",
                "matrix": [
                    {"id": "spellcheck", "control": "spellCheck", "value": True},
                    {"id": "spellcheck", "control": "spellCheck", "value": False},
                ],
            }
        }
    )

    assert errors == []
    assert [case["id"] for case in generated] == [
        "spellcheck.true",
        "spellcheck.false",
    ]
    assert generated[0]["transitions"][0]["action"] == {
        "kind": "ui.key_sequence",
        "selector": {"automation_id": "spellCheck"},
        "keys": "{SPACE}",
    }
    assert generated[0] != generated[1]


def test_setting_row_effect_template_supports_realize_rows() -> None:
    generated, errors = expand_generated_cases(
        {
            "generate": {
                "template": "setting-ab-row-effect",
                "matrix": [
                    {
                        "id": "media_enabled",
                        "control": "mediaToggle",
                        "value": True,
                        "row_index": 750,
                        "row_expected": {"Title": "Clip 750", "Enabled": True},
                        "grid": "mediaGrid",
                        "realize": True,
                    }
                ],
            }
        }
    )

    assert errors == []
    case = generated[0]
    assert case["id"] == "media_enabled.row-750.true"
    assert case["transitions"][0]["action"]["realize"] is True
    assert case["transitions"][0]["probes"][1] == {
        "kind": "ui.grid",
        "name": "row_effect",
        "selector": {"automation_id": "mediaGrid"},
        "rows": [{"Title": "Clip 750", "Enabled": True}],
        "columns": [],
    }


def test_setting_row_effect_template_normalizes_scalar_columns() -> None:
    generated, errors = expand_generated_cases(
        {
            "generate": {
                "template": "setting-ab-row-effect",
                "matrix": [
                    {
                        "id": "media_enabled",
                        "control": "mediaToggle",
                        "value": True,
                        "row_index": 750,
                        "grid": "mediaGrid",
                        "columns": "Title",
                    }
                ],
            }
        }
    )

    assert errors == []
    assert generated[0]["transitions"][0]["probes"][1]["columns"] == ["Title"]


def test_radio_group_template_asserts_siblings_by_default() -> None:
    generated, errors = expand_generated_cases(
        {
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
                    }
                ],
            }
        }
    )

    assert errors == []
    probes = generated[0]["transitions"][0]["probes"]
    assert generated[0]["transitions"][0]["action"] == {
        "kind": "ui.key_sequence",
        "selector": {"automation_id": "checkBoxGeneric"},
        "keys": "{SPACE}",
    }
    assert [probe["expected"] for probe in probes] == ["generic", True, False, False]


def test_radio_group_template_uses_target_as_debug_expected_value() -> None:
    generated, errors = expand_generated_cases(
        {
            "generate": {
                "template": "radio-group-set",
                "matrix": [
                    {
                        "id": "mode_scanning",
                        "value": "generic",
                        "target": "scanning",
                        "expression": "ViewModel.Mode",
                        "controls": [
                            {"value": "generic", "automation_id": "checkBoxGeneric"},
                            {"value": "scanning", "automation_id": "checkBoxScanning"},
                        ],
                    }
                ],
            }
        }
    )

    assert errors == []
    probes = generated[0]["transitions"][0]["probes"]
    assert probes[0]["expected"] == "scanning"


def test_placeholder_substituter_rejects_unknown_without_recursive_rendering() -> None:
    assert render_template_value("case-{id}-{value}", {"id": "a", "value": True}) == ("case-a-true")
    assert render_template_value("{value}", {"value": "{not_recursive}"}) == ("{not_recursive}")
    with pytest.raises(TemplateRenderError, match="unknown placeholder"):
        render_template_value("{missing}", {"id": "a"})


def test_placeholder_substituter_wraps_malformed_template() -> None:
    with pytest.raises(TemplateRenderError, match="malformed template"):
        render_template_value("{broken", {"id": "a"})
