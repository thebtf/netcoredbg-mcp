from __future__ import annotations

import pytest

from netcoredbg_mcp.session.runtime_smoke_v2.generate import expand_generated_cases
from netcoredbg_mcp.session.runtime_smoke_v2.templates import accepted_template_names
from netcoredbg_mcp.session.runtime_smoke_v2.templates._substituter import (
    TemplateRenderError,
    render_template_value,
)


def test_template_registry_exposes_builtin_templates() -> None:
    assert accepted_template_names() == [
        "novascript-action-oracle",
        "radio-group-set",
        "setting-ab-row-effect",
        "state-only-file-json",
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


def test_novascript_action_oracle_template_generates_route_action_and_file_oracles() -> None:
    generated, errors = expand_generated_cases(
        {
            "generate": {
                "template": "novascript-action-oracle",
                "matrix": [
                    {
                        "id": "route_apply",
                        "action": {
                            "kind": "ui.invoke",
                            "selector": {"automation_id": "applyButton"},
                        },
                        "settle": {"idle_ms": 0},
                        "oracles": [
                            {
                                "name": "route",
                                "path": "Tmp/evidence/{id}.json",
                                "jsonpath": "$.route",
                                "expected": "apply",
                            },
                            {
                                "name": "verdict",
                                "path": "Tmp/evidence/{id}.json",
                                "jsonpath": "$.verdict",
                                "expected": "PASS",
                            },
                        ],
                    }
                ],
            }
        }
    )

    assert errors == []
    case = generated[0]
    assert case["id"] == "route_apply"
    assert case["transitions"][0]["action"] == {
        "kind": "ui.invoke",
        "selector": {"automation_id": "applyButton"},
    }
    assert case["transitions"][0]["settle"] == {"idle_ms": 0}
    assert case["transitions"][0]["probes"] == [
        {
            "kind": "file.json",
            "phase": "after",
            "name": "route",
            "path": "Tmp/evidence/route_apply.json",
            "jsonpath": "$.route",
            "expected": "apply",
        },
        {
            "kind": "file.json",
            "phase": "after",
            "name": "verdict",
            "path": "Tmp/evidence/route_apply.json",
            "jsonpath": "$.verdict",
            "expected": "PASS",
        },
    ]


def test_novascript_action_oracle_template_generates_app_diagnostics_probe() -> None:
    generated, errors = expand_generated_cases(
        {
            "generate": {
                "template": "novascript-action-oracle",
                "matrix": [
                    {
                        "id": "route_apply",
                        "action": {"kind": "wait", "idle_ms": 0},
                        "oracles": [
                            {
                                "kind": "app_diagnostics",
                                "name": "diagnostic",
                                "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
                                "app": {"name": "PlaceholderApp"},
                                "status": "PASS",
                                "observations": [],
                                "redaction": {"omit_fields": ["raw_tree"]},
                                "limits": {
                                    "max_text_length": 240,
                                    "max_list_items": 8,
                                    "max_json_bytes": 32768,
                                },
                                "poll": {
                                    "path": "Tmp/evidence/{id}.json",
                                    "timeout_ms": 0,
                                    "poll_interval_ms": 0,
                                },
                            }
                        ],
                    }
                ],
            }
        }
    )

    assert errors == []
    probe = generated[0]["transitions"][0]["probes"][0]
    assert probe["kind"] == "app_diagnostics"
    assert probe["name"] == "diagnostic"
    assert probe["phase"] == "after"
    assert probe["schema"] == "netcoredbg.runtime_smoke.diagnostics.v1"
    assert probe["app"] == {"name": "PlaceholderApp"}
    assert probe["poll"] == {
        "path": "Tmp/evidence/route_apply.json",
        "timeout_ms": 0,
        "poll_interval_ms": 0,
    }
