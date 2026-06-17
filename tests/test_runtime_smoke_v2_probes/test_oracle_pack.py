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


async def _read_status_text(**_: Any) -> dict[str, Any]:
    return {
        "status": "PASS",
        "text": "Ready",
        "source": "extract_text",
        "evidence_ref": "ui:text:StatusLabel",
    }


async def _read_status_property(**_: Any) -> dict[str, Any]:
    return {
        "status": "PASS",
        "value": "Ready",
        "source": "property",
        "evidence_ref": "ui:property:StatusLabel.Value",
    }


async def _read_disagreeing_status_property(**_: Any) -> dict[str, Any]:
    return {
        "status": "PASS",
        "value": "Busy",
        "source": "property",
        "evidence_ref": "ui:property:StatusLabel.Value",
    }


async def _read_status_text_or_fail(selector: dict[str, Any], **_: Any) -> dict[str, Any]:
    if selector.get("automation_id") == "MissingStatusLabel":
        return {
            "status": "FAIL",
            "reason": "status text unavailable",
            "value": None,
            "evidence_ref": "ui:text:MissingStatusLabel",
        }
    return await _read_status_text()


async def _read_status_text_impasse(**_: Any) -> dict[str, Any]:
    return {
        "status": "IMPASSE",
        "reason": "status text unavailable after retry budget",
        "value": None,
        "evidence_ref": "ui:text:StatusLabel",
    }


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
async def test_oracle_pack_probe_runs_named_sources_and_returns_source_evidence() -> None:
    result = await runner(
        ProbeSmokeSession(),
        adapters={
            "ui.text.read": _read_status_text,
            "ui.get_property": _read_status_property,
        },
    ).run(
        one_probe_plan(
            _oracle_pack(
                phase="after",
                sources=[
                    {
                        "id": "status_text",
                        "probe": {
                            "kind": "ui.text",
                            "action": "read",
                            "selector": {"automation_id": "StatusLabel"},
                        },
                    },
                    {
                        "id": "status_property",
                        "probe": {
                            "kind": "ui.property",
                            "selector": {"automation_id": "StatusLabel"},
                            "property": "Value",
                        },
                    },
                ],
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["source_count"] == 2
    assert probe["value"]["sources"] == [
        {
            "id": "status_text",
            "kind": "ui.text",
            "status": "PASS",
            "value": "Ready",
            "evidence_ref": "ui:text:StatusLabel",
        },
        {
            "id": "status_property",
            "kind": "ui.property",
            "status": "PASS",
            "value": "Ready",
        },
    ]


@pytest.mark.asyncio
async def test_oracle_pack_probe_blocks_with_disagreeing_sources() -> None:
    result = await runner(
        ProbeSmokeSession(),
        adapters={
            "ui.text.read": _read_status_text,
            "ui.get_property": _read_disagreeing_status_property,
        },
    ).run(
        one_probe_plan(
            _oracle_pack(
                phase="after",
                sources=[
                    {
                        "id": "status_text",
                        "probe": {
                            "kind": "ui.text",
                            "action": "read",
                            "selector": {"automation_id": "StatusLabel"},
                        },
                    },
                    {
                        "id": "status_property",
                        "probe": {
                            "kind": "ui.property",
                            "selector": {"automation_id": "StatusLabel"},
                            "property": "Value",
                        },
                    },
                ],
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "DISAGREEING_SOURCES"
    assert probe["classification"] == "DISAGREEING_SOURCES"
    assert probe["next_step"] == "Inspect source evidence and fix the disagreeing oracle inputs."
    assert probe["value"]["source_values"] == {
        "status_text": "Ready",
        "status_property": "Busy",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("BLOCKED", "oracle pack reported BLOCKED"),
        ("FAIL", "oracle pack reported FAIL"),
    ],
)
async def test_oracle_pack_probe_preserves_explicit_non_pass_status_with_disagreement(
    status: str,
    reason: str,
) -> None:
    result = await runner(
        ProbeSmokeSession(),
        adapters={
            "ui.text.read": _read_status_text,
            "ui.get_property": _read_disagreeing_status_property,
        },
    ).run(
        one_probe_plan(
            _oracle_pack(
                phase="after",
                status=status,
                sources=[
                    {
                        "id": "status_text",
                        "probe": {
                            "kind": "ui.text",
                            "action": "read",
                            "selector": {"automation_id": "StatusLabel"},
                        },
                    },
                    {
                        "id": "status_property",
                        "probe": {
                            "kind": "ui.property",
                            "selector": {"automation_id": "StatusLabel"},
                            "property": "Value",
                        },
                    },
                ],
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == status
    assert probe["status"] == status
    assert probe["reason"] == reason
    assert "classification" not in probe
    assert "source_values" not in probe["value"]


@pytest.mark.asyncio
async def test_oracle_pack_probe_preserves_source_failure_over_disagreement() -> None:
    result = await runner(
        ProbeSmokeSession(),
        adapters={
            "ui.text.read": _read_status_text_or_fail,
            "ui.get_property": _read_disagreeing_status_property,
        },
    ).run(
        one_probe_plan(
            _oracle_pack(
                phase="after",
                sources=[
                    {
                        "id": "status_text",
                        "probe": {
                            "kind": "ui.text",
                            "action": "read",
                            "selector": {"automation_id": "StatusLabel"},
                        },
                    },
                    {
                        "id": "status_property",
                        "probe": {
                            "kind": "ui.property",
                            "selector": {"automation_id": "StatusLabel"},
                            "property": "Value",
                        },
                    },
                    {
                        "id": "missing_status_text",
                        "probe": {
                            "kind": "ui.text",
                            "action": "read",
                            "selector": {"automation_id": "MissingStatusLabel"},
                        },
                    },
                ],
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "FAIL"
    assert probe["status"] == "FAIL"
    assert probe["reason"] == "oracle source failed"
    assert "classification" not in probe
    assert "source_values" not in probe["value"]
    assert probe["value"]["sources"][2]["status"] == "FAIL"


@pytest.mark.asyncio
async def test_oracle_pack_probe_preserves_impasse_source_status() -> None:
    result = await runner(
        ProbeSmokeSession(),
        adapters={"ui.text.read": _read_status_text_impasse},
    ).run(
        one_probe_plan(
            _oracle_pack(
                phase="after",
                sources=[
                    {
                        "id": "status_text",
                        "probe": {
                            "kind": "ui.text",
                            "action": "read",
                            "selector": {"automation_id": "StatusLabel"},
                        },
                    }
                ],
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "IMPASSE"
    assert probe["status"] == "IMPASSE"
    assert probe["reason"] == "oracle source impasse"
    assert probe["value"]["sources"][0]["status"] == "IMPASSE"


@pytest.mark.asyncio
async def test_oracle_pack_probe_blocks_when_source_is_blocked() -> None:
    result = await runner(ProbeSmokeSession()).run(
        one_probe_plan(
            _oracle_pack(
                phase="after",
                sources=[
                    {
                        "id": "status_text",
                        "probe": {
                            "kind": "ui.text",
                            "action": "read",
                            "selector": {"automation_id": "StatusLabel"},
                        },
                    }
                ],
            )
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "oracle source blocked"
    assert probe["value"]["sources"][0]["status"] == "BLOCKED"
    assert probe["next_step"] == "Inspect source evidence and make every source runnable."


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
