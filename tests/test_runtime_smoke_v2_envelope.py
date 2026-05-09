from __future__ import annotations

from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession


class EnvelopeSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.launch_calls = 0

    async def launch(self, **_: Any) -> dict[str, Any]:
        self.launch_calls += 1
        return {"status": "PASS", "reason": "launched"}


@pytest.mark.asyncio
async def test_v2_plan_rejects_mixed_legacy_execution_keys_before_launch() -> None:
    session = EnvelopeSmokeSession()

    result = await RuntimeSmokeRunner(session).run({
        "schema": "netcoredbg.runtime_smoke.v2",
        "name": "mixed schema plan",
        "launch": {"program": "must-not-run.exe"},
        "cases": [],
        "steps": [],
        "actions": [],
        "assertions": [],
    })

    assert result["status"] == "FAIL"
    assert result["reason"] == "invalid plan schema"
    assert session.launch_calls == 0

    errors_text = "\n".join(result["validation_errors"])
    assert "steps" in errors_text
    assert "actions" in errors_text
    assert "assertions" in errors_text

    assert {"baseline", "generate", "cases", "metrics_thresholds"}.issubset(
        set(result["accepted_top_level_keys_v2"])
    )
