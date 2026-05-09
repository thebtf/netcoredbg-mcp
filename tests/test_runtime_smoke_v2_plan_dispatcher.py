from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.runtime_smoke_v2.probe_dispatcher import probe_path


class DispatcherSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()

    async def launch(self, **_: Any) -> dict[str, Any]:
        return {"status": "PASS", "reason": "launched"}


@pytest.mark.asyncio
async def test_v2_schema_dispatches_to_v2_result_envelope() -> None:
    result = await RuntimeSmokeRunner(DispatcherSmokeSession()).run({
        "schema": "netcoredbg.runtime_smoke.v2",
        "name": "state oracle smoke",
    })

    assert result["status"] == "BLOCKED"
    assert result["generated_case_count"] == 0
    assert result["cases"] == []
    assert result["cleanup"]["status"] == "PASS"
    assert "accepted_schema_values" in result


@pytest.mark.asyncio
async def test_v1_schema_keeps_legacy_result_envelope() -> None:
    result = await RuntimeSmokeRunner(DispatcherSmokeSession()).run({
        "schema": "netcoredbg.runtime_smoke.v1",
        "name": "legacy smoke",
    })

    assert result["status"] == "PASS"
    assert result["reason"] == "runtime smoke scenario passed"
    assert result["completed_steps"] == []
    assert result["failed_assertions"] == []
    assert "generated_case_count" not in result
    assert "cases" not in result


@pytest.mark.asyncio
async def test_missing_schema_with_v2_only_keys_returns_schema_help() -> None:
    result = await RuntimeSmokeRunner(DispatcherSmokeSession()).run({
        "name": "missing schema but v2 shaped",
        "cases": [],
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert result["accepted_schema_values"] == [
        "netcoredbg.runtime_smoke.v1",
        "netcoredbg.runtime_smoke.v2",
    ]


def test_probe_path_uses_kind_without_duplicating_missing_name() -> None:
    assert probe_path({"kind": "ui.grid"}) == "ui.grid"
    assert probe_path({"kind": "ui.grid", "name": "row_effect"}) == "ui.grid.row_effect"
