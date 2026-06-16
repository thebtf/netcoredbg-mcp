from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke_schema import (
    DIAGNOSTIC_EVIDENCE_LIMITS,
    DIAGNOSTIC_SCHEMA_VERSION,
)
from netcoredbg_mcp.session.runtime_smoke_v2.runner import validate_v2_plan_contract

from .helpers import ProbeSmokeSession, after_probe, one_probe_plan, runner


def _limits() -> dict[str, int]:
    return dict(DIAGNOSTIC_EVIDENCE_LIMITS)


def _app_diagnostics(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "app_diagnostics",
        "schema": DIAGNOSTIC_SCHEMA_VERSION,
        "app": {
            "name": "WpfSmokeApp",
            "process_name": "dotnet",
            "expected_modules": ["WpfSmokeApp.dll"],
        },
        "status": "BLOCKED",
        "observations": [
            {
                "kind": "ui.backend",
                "status": "BLOCKED",
                "reason": "GridPattern unavailable",
                "requested": {"control_type": "DataGrid"},
                "accepted": {"fallback": "bounded descendant text"},
                "next_step": "Run the WPF fixture replay on a GUI worker.",
            }
        ],
        "redaction": {"omit_fields": ["raw_tree", "screenshot_base64", "secret"]},
        "limits": _limits(),
    }
    payload.update(overrides)
    return payload


def test_validate_v2_plan_contract_accepts_app_diagnostics_probe_kind() -> None:
    validation = validate_v2_plan_contract(one_probe_plan(_app_diagnostics()))

    assert validation["validation_errors"] == []
    assert "app_diagnostics" in validation["accepted_probe_kinds"]


@pytest.mark.asyncio
async def test_app_diagnostics_probe_returns_bounded_blocked_observations() -> None:
    result = await runner(ProbeSmokeSession()).run(one_probe_plan(_app_diagnostics()))

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["kind"] == "app_diagnostics"
    assert probe["reason"] == "GridPattern unavailable"
    assert probe["value"]["schema"] == DIAGNOSTIC_SCHEMA_VERSION
    assert probe["value"]["app"]["name"] == "WpfSmokeApp"
    assert probe["value"]["observation_count"] == 1
    observation = probe["value"]["observations"][0]
    assert observation["reason"] == "GridPattern unavailable"
    assert observation["requested"] == {"control_type": "DataGrid"}
    assert observation["accepted"] == {"fallback": "bounded descendant text"}
    assert observation["next_step"].startswith("Run the WPF fixture replay")
    assert "raw_tree" not in str(probe["value"])
    assert "screenshot_base64" not in str(probe["value"])
    assert "secret" not in str(probe["value"])


@pytest.mark.asyncio
async def test_app_diagnostics_probe_applies_probe_specific_evidence_limits() -> None:
    long_reason = "backend diagnostic text exceeds strict limit"
    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                observations=[
                    {
                        "kind": "ui.backend",
                        "status": "BLOCKED",
                        "reason": long_reason,
                        "requested": {"control_type": "DataGrid"},
                        "accepted": {"fallback": "bounded descendant text"},
                        "next_step": "Inspect the bounded evidence.",
                    },
                    {
                        "kind": "artifact",
                        "status": "PASS",
                        "reason": "second observation",
                        "next_step": "No action required.",
                    },
                ],
                limits={
                    **_limits(),
                    "max_text_length": 12,
                    "max_list_items": 1,
                },
            )
        )
    )

    observations = after_probe(result)["value"]["observations"]
    assert "reason" not in observations[0]
    assert observations[0]["reason_length"] == len(long_reason)
    assert observations[0]["next_step_length"] == len("Inspect the bounded evidence.")
    assert observations[0]["omitted_fields"] == ["reason", "next_step"]
    assert observations[1] == {"omitted_count": 1}


@pytest.mark.asyncio
async def test_app_diagnostics_probe_blocks_invalid_unsafe_evidence() -> None:
    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _app_diagnostics(
                observations=[
                    {
                        "kind": "ui.backend",
                        "status": "BLOCKED",
                        "reason": "Tree dump leaked",
                        "requested": {"raw_tree": {"name": "Window"}},
                        "accepted": {"fallback": "bounded descendant text"},
                        "next_step": "Use summarized UI evidence.",
                    }
                ]
            )
        )
    )

    assert result["status"] == "INVALID_SETUP"
    assert result["reason"] == "invalid plan schema"
    assert "app_diagnostics.observations[0].requested.raw_tree must be omitted" in (
        "\n".join(result["validation_errors"])
    )
    assert result["cases"] == []
