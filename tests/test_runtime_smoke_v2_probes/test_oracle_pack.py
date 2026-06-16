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


def _oracle_pack(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "oracle_pack",
        "schema": DIAGNOSTIC_SCHEMA_VERSION,
        "id": "wpf-grid-oracle-pack",
        "status": "PASS",
        "checks": [
            {
                "id": "visible-row-count",
                "probe": "ui.grid",
                "expect": {"min_rows": 1, "identity": {"column": "Phrase"}},
                "on_blocked": {
                    "next_step": "Replay with a UI backend that exposes GridPattern.",
                },
            }
        ],
        "limits": _limits(),
        "redaction": {"omit_fields": ["raw_tree", "screenshot_base64", "secret"]},
    }
    payload.update(overrides)
    return payload


def test_validate_v2_plan_contract_accepts_oracle_pack_probe_kind() -> None:
    validation = validate_v2_plan_contract(one_probe_plan(_oracle_pack()))

    assert validation["validation_errors"] == []
    assert "oracle_pack" in validation["accepted_probe_kinds"]


@pytest.mark.asyncio
async def test_oracle_pack_probe_executes_checks_and_returns_bounded_value() -> None:
    result = await runner(ProbeSmokeSession()).run(one_probe_plan(_oracle_pack()))

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["kind"] == "oracle_pack"
    assert probe["value"]["schema"] == DIAGNOSTIC_SCHEMA_VERSION
    assert probe["value"]["id"] == "wpf-grid-oracle-pack"
    assert probe["value"]["check_count"] == 1
    assert probe["value"]["checks"][0]["id"] == "visible-row-count"
    assert probe["value"]["checks"][0]["probe"] == "ui.grid"
    assert "raw_tree" not in str(probe["value"])
    assert "screenshot_base64" not in str(probe["value"])


@pytest.mark.asyncio
async def test_oracle_pack_probe_applies_probe_specific_evidence_limits() -> None:
    long_text = "selector diagnostic text exceeds strict limit"
    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _oracle_pack(
                checks=[
                    {
                        "id": "strict-first-check",
                        "probe": "ui.grid",
                        "expect": {"diagnostic": long_text},
                        "on_blocked": {"next_step": "Inspect the bounded evidence."},
                    },
                    {
                        "id": "strict-second-check",
                        "probe": "ui.grid",
                        "expect": {"min_rows": 1},
                        "on_blocked": {"next_step": "Inspect the second check."},
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

    checks = after_probe(result)["value"]["checks"]
    assert "diagnostic" not in checks[0]["expect"]
    assert checks[0]["expect"]["diagnostic_length"] == len(long_text)
    assert checks[0]["expect"]["omitted_fields"] == ["diagnostic"]
    assert checks[1] == {"omitted_count": 1}


@pytest.mark.asyncio
async def test_oracle_pack_probe_blocks_invalid_pack_with_schema_errors() -> None:
    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _oracle_pack(
                checks=[
                    {
                        "id": "missing-next-step",
                        "probe": "ui.grid",
                        "expect": {"min_rows": 1},
                        "on_blocked": {},
                    }
                ],
            )
        )
    )

    assert result["status"] == "INVALID_SETUP"
    assert result["reason"] == "invalid plan schema"
    assert "oracle_pack.checks[0].on_blocked.next_step is required" in result[
        "validation_errors"
    ]
    assert result["cases"] == []
