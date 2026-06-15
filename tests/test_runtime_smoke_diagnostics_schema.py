"""Runtime-smoke diagnostic schema contract tests."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_diagnostic_schema_contract_exposes_status_limits_and_redaction() -> None:
    from netcoredbg_mcp.session.runtime_smoke_schema import diagnostic_schema_contract
    from netcoredbg_mcp.session.runtime_smoke_v2.result_envelope import (
        MAX_COMPACT_LIST_ITEMS,
        MAX_COMPACT_TEXT_LENGTH,
    )
    from netcoredbg_mcp.session.tracepoints import (
        EVALUATE_TIMEOUT_SECONDS,
        MAX_TRACE_ENTRIES,
        RATE_LIMIT_INTERVAL_SECONDS,
    )

    contract = diagnostic_schema_contract()

    assert contract["schema"] == "netcoredbg.runtime_smoke.diagnostics.v1"
    assert contract["status_values"] == ["PASS", "BLOCKED", "FAIL"]
    assert contract["evidence_limits"] == {
        "max_text_length": MAX_COMPACT_TEXT_LENGTH,
        "max_list_items": MAX_COMPACT_LIST_ITEMS,
        "max_json_bytes": 32768,
    }

    omitted = set(contract["redaction"]["omit_fields"])
    assert {
        "raw_tree",
        "window_tree",
        "ui_tree",
        "screenshot_base64",
        "access_token",
        "password",
        "secret",
    }.issubset(omitted)
    assert {"backend_result", "exception", "raw_output", "stack"}.issubset(
        set(contract["redaction"]["summarize_fields"])
    )

    assert "checks" in contract["oracle_pack"]["required_fields"]
    assert "observations" in contract["app_diagnostics"]["required_fields"]
    assert "on_blocked" in contract["semantic_probe"]["required_fields"]
    assert "cleanup" in contract["tracepoint_guardrail"]["required_fields"]
    for section_name in (
        "oracle_pack",
        "app_diagnostics",
        "semantic_probe",
        "tracepoint_guardrail",
    ):
        section = contract[section_name]
        assert section["required_fields"]
        assert section["optional_fields"]

    assert contract["oracle_pack"]["failure_modes"]
    assert contract["app_diagnostics"]["failure_modes"]
    assert contract["semantic_probe"]["failure_modes"]
    assert contract["tracepoint_guardrail"]["mode_values"] == ["allow", "block", "unsafe"]
    assert "debug.tracepoint.remove" in contract["tracepoint_guardrail"]["cleanup_operations"]
    assert contract["tracepoint_guardrail"]["runtime_limits"] == {
        "max_trace_entries": MAX_TRACE_ENTRIES,
        "evaluate_timeout_seconds": EVALUATE_TIMEOUT_SECONDS,
        "rate_limit_interval_seconds": RATE_LIMIT_INTERVAL_SECONDS,
    }


def test_diagnostic_schema_contract_matches_runtime_probe_registry() -> None:
    from netcoredbg_mcp.session.runtime_smoke_schema import diagnostic_schema_contract
    from netcoredbg_mcp.session.runtime_smoke_v2.probes import accepted_probe_kinds

    contract = diagnostic_schema_contract()

    assert contract["semantic_probe"]["probe_kinds"] == accepted_probe_kinds()


def test_diagnostic_schema_examples_validate() -> None:
    from netcoredbg_mcp.session.runtime_smoke_schema import (
        validate_diagnostic_schema_example,
    )

    examples = {
        "oracle_pack": REPO_ROOT / "docs/examples/runtime-smoke-oracle-pack.json",
        "app_diagnostics": REPO_ROOT / "docs/examples/runtime-smoke-app-diagnostics.json",
        "semantic_probe": REPO_ROOT / "docs/examples/runtime-smoke-semantic-probe.json",
        "tracepoint_guardrail": REPO_ROOT
        / "docs/examples/runtime-smoke-tracepoint-guardrail.json",
    }

    for kind, path in examples.items():
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        assert validate_diagnostic_schema_example(payload, kind=kind) == []


def test_diagnostic_schema_examples_fail_closed_with_actionable_errors() -> None:
    from netcoredbg_mcp.session.runtime_smoke_schema import (
        validate_diagnostic_schema_example,
    )

    assert validate_diagnostic_schema_example({}, kind="oracle_pack") == [
        "oracle_pack.schema is required",
        "oracle_pack.id is required",
        "oracle_pack.status is required",
        "oracle_pack.checks is required",
        "oracle_pack.limits is required",
    ]
    tracepoint_errors = validate_diagnostic_schema_example(
        {
            "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
            "id": "unsafe-tracepoints",
            "status": "MAYBE",
            "cleanup": {},
        },
        kind="tracepoint_guardrail",
    )
    for expected_error in [
        "tracepoint_guardrail.status must be one of: PASS, BLOCKED, FAIL",
        "tracepoint_guardrail.mode is required",
        "tracepoint_guardrail.allowed_when is required",
        "tracepoint_guardrail.blocked_when is required",
        "tracepoint_guardrail.unsafe_when is required",
        "tracepoint_guardrail.cleanup.owner is required",
        "tracepoint_guardrail.cleanup.operations must be a list of strings",
    ]:
        assert expected_error in tracepoint_errors


def test_oracle_pack_schema_rejects_unactionable_checks() -> None:
    from netcoredbg_mcp.session.runtime_smoke_schema import (
        validate_diagnostic_schema_example,
    )
    from netcoredbg_mcp.session.runtime_smoke_v2.probes import accepted_probe_kinds

    payload = {
        "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
        "id": "bad-oracle-pack",
        "status": "BLOCKED",
        "checks": [
            {
                "probe": "ui.colorscheme",
                "on_blocked": {},
            }
        ],
        "limits": {
            "max_text_length": 240,
            "max_list_items": 8,
            "max_json_bytes": 32768,
        },
    }

    errors = validate_diagnostic_schema_example(payload, kind="oracle_pack")

    assert "oracle_pack.checks[0].id is required" in errors
    assert "oracle_pack.checks[0].expect is required" in errors
    assert "oracle_pack.checks[0].on_blocked.next_step is required" in errors
    assert any(
        error.startswith("oracle_pack.checks[0].probe must be one of: ")
        for error in errors
    )
    assert "ui.colorscheme" not in accepted_probe_kinds()

    payload["checks"][0]["probe"] = "ui.text"
    payload["checks"][0]["expect"] = {}
    payload["checks"][0]["on_blocked"] = "inspect the selector"
    errors = validate_diagnostic_schema_example(payload, kind="oracle_pack")

    assert "oracle_pack.checks[0].on_blocked must be an object" in errors


def test_diagnostic_schema_rejects_negative_evidence_limits() -> None:
    from netcoredbg_mcp.session.runtime_smoke_schema import (
        validate_diagnostic_schema_example,
    )

    payload = {
        "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
        "id": "bad-limits",
        "status": "BLOCKED",
        "checks": [],
        "limits": {
            "max_text_length": -1,
            "max_list_items": -1,
            "max_json_bytes": -1,
        },
    }

    errors = validate_diagnostic_schema_example(payload, kind="oracle_pack")

    assert "oracle_pack.limits.max_text_length must be >= 0" in errors
    assert "oracle_pack.limits.max_list_items must be >= 0" in errors
    assert "oracle_pack.limits.max_json_bytes must be >= 0" in errors


def test_app_diagnostics_schema_rejects_unactionable_blocked_observations() -> None:
    from netcoredbg_mcp.session.runtime_smoke_schema import (
        validate_diagnostic_schema_example,
    )

    payload = {
        "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
        "app": {"name": "WpfSmokeApp"},
        "status": "BLOCKED",
        "observations": [
            {
                "kind": "ui.backend",
                "status": "BLOCKED",
                "raw_tree": {"Window": []},
                "screenshot_base64": "not-safe",
            }
        ],
        "redaction": {"omit_fields": ["raw_tree", "screenshot_base64"]},
        "limits": {
            "max_text_length": 240,
            "max_list_items": 8,
            "max_json_bytes": 32768,
        },
    }

    errors = validate_diagnostic_schema_example(payload, kind="app_diagnostics")

    assert "app_diagnostics.observations[0].reason is required for BLOCKED" in errors
    assert "app_diagnostics.observations[0].requested is required for BLOCKED" in errors
    assert "app_diagnostics.observations[0].accepted is required for BLOCKED" in errors
    assert "app_diagnostics.observations[0].next_step is required for BLOCKED" in errors
    assert "app_diagnostics.observations[0].raw_tree must be omitted or summarized" in errors
    assert (
        "app_diagnostics.observations[0].screenshot_base64 must be omitted or summarized"
        in errors
    )


def test_semantic_probe_schema_rejects_unknown_probe_and_incomplete_on_blocked() -> None:
    from netcoredbg_mcp.session.runtime_smoke_schema import (
        validate_diagnostic_schema_example,
    )

    payload = {
        "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
        "kind": "ui.colorscheme",
        "status": "BLOCKED",
        "selector": {"automation_id": "Theme"},
        "expect": {"value": "dark"},
        "on_blocked": {"reason": "not implemented"},
        "backend_result": {
            "status": "FAIL",
            "raw_tree": {"Window": []},
            "screenshot_base64": "not-safe",
        },
    }

    errors = validate_diagnostic_schema_example(payload, kind="semantic_probe")

    assert any(error.startswith("semantic_probe.kind must be one of: ") for error in errors)
    assert "semantic_probe.on_blocked.requested is required for BLOCKED" in errors
    assert "semantic_probe.on_blocked.accepted is required for BLOCKED" in errors
    assert "semantic_probe.on_blocked.next_step is required for BLOCKED" in errors
    assert "semantic_probe.backend_result.raw_tree must be omitted or summarized" in errors
    assert (
        "semantic_probe.backend_result.screenshot_base64 must be omitted or summarized"
        in errors
    )


def test_tracepoint_guardrail_requires_cleanup_ownership() -> None:
    from netcoredbg_mcp.session.runtime_smoke_schema import (
        validate_diagnostic_schema_example,
    )

    payload = {
        "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
        "id": "unsafe-tracepoint",
        "status": "BLOCKED",
        "mode": "maybe",
        "allowed_when": [],
        "blocked_when": [],
        "unsafe_when": [],
        "cleanup": {
            "required": True,
            "operations": ["process.registry.assert_empty"],
        },
    }

    errors = validate_diagnostic_schema_example(payload, kind="tracepoint_guardrail")

    assert "tracepoint_guardrail.mode must be one of: allow, block, unsafe" in errors
    assert "tracepoint_guardrail.allowed_when must not be empty" in errors
    assert "tracepoint_guardrail.blocked_when must not be empty" in errors
    assert "tracepoint_guardrail.unsafe_when must not be empty" in errors
    assert (
        "tracepoint_guardrail.cleanup.operations must include debug.tracepoint.remove"
        in errors
    )

    payload["allowed_when"] = ["safe", 123]
    payload["blocked_when"] = ["blocked", {"reason": "bad"}]
    payload["unsafe_when"] = ["unsafe", None]
    errors = validate_diagnostic_schema_example(payload, kind="tracepoint_guardrail")

    assert "tracepoint_guardrail.allowed_when must be a list of strings" in errors
    assert "tracepoint_guardrail.blocked_when must be a list of strings" in errors
    assert "tracepoint_guardrail.unsafe_when must be a list of strings" in errors
