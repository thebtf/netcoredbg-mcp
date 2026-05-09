from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession
from netcoredbg_mcp.session.state import DebugState


class EnvelopeSmokeSession:
    def __init__(self) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.STOPPED,
            output_buffer=[],
            process_id=None,
            process_name=None,
            modules=[],
            loaded_sources={},
        )

    async def find_element(self, selector: dict[str, Any]) -> dict[str, Any]:
        return {"status": "PASS", "found": True}

    async def set_focus(self, selector: dict[str, Any]) -> dict[str, Any]:
        return {"status": "PASS"}

    async def send_keys_focused(self, keys: str) -> dict[str, Any]:
        return {"status": "PASS"}

    async def evaluate(self, expression: str) -> dict[str, Any]:
        return {"status": "PASS", "value": True}

    async def get_property(
        self,
        *,
        selector: dict[str, Any],
        property_name: str,
    ) -> dict[str, Any]:
        return {
            "status": "PASS",
            "found": True,
            "value": True,
            "raw_output": "x" * 1000,
            "screenshot_path": f"screenshots/{selector['automation_id']}.png",
        }


def _runner(session: EnvelopeSmokeSession) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.find_element": session.find_element,
            "ui.set_focus": session.set_focus,
            "ui.send_keys_focused": session.send_keys_focused,
            "debug.evaluate": session.evaluate,
            "ui.get_property": session.get_property,
        },
    )


@pytest.mark.asyncio
async def test_v2_compact_section_stays_under_four_kb_for_generated_cases() -> None:
    session = EnvelopeSmokeSession()
    result = await _runner(session).run(
        {
            "schema": "netcoredbg.runtime_smoke.v2",
            "generate": {
                "template": "toggle-setting-ab",
                "matrix": [
                    {
                        "id": f"setting_{index}",
                        "control": f"checkBox{index}",
                        "value": True,
                        "setting_expression": f"Settings.Flag{index}.true",
                    }
                    for index in range(18)
                ],
            },
        }
    )

    compact_json = json.dumps(result["compact"], sort_keys=True, separators=(",", ":"))
    assert result["status"] == "PASS"
    assert result["generated_case_count"] == 18
    assert len(compact_json.encode("utf-8")) < 4096
    assert "cases" not in result["compact"]
    assert result["compact"]["case_count"] == 18


@pytest.mark.asyncio
async def test_v1_runtime_smoke_result_shape_stays_compatible() -> None:
    session = EnvelopeSmokeSession()

    result = await RuntimeSmokeRunner(session).run({"name": "release-critical"})

    assert list(result) == [
        "status",
        "reason",
        "elapsed_ms",
        "action_count",
        "completed_steps",
        "failed_assertions",
        "cleanup",
        "evidence_refs",
        "compact",
    ]
    assert result["compact"]["cleanup"]["status"] == result["cleanup"]["status"]
