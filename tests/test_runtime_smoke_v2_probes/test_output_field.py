from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from .helpers import ProbeSmokeSession, after_probe, one_probe_plan, runner


class OutputFieldProbeSession(ProbeSmokeSession):
    def __init__(self) -> None:
        super().__init__()
        self.lines_results: deque[dict[str, Any]] = deque()

    async def output_lines_since(self, *, checkpoint: str) -> dict[str, Any]:
        self.calls.append(("output.lines_since", checkpoint))
        return self.lines_results.popleft()


@pytest.mark.asyncio
async def test_output_field_probe_extracts_structured_field() -> None:
    session = OutputFieldProbeSession()
    session.lines_results.extend(
        [
            {
                "status": "PASS",
                "lines": ["source=SettingsOracle reason=audio-filter-chain audioFilter="],
            },
            {
                "status": "PASS",
                "lines": [
                    "source=SettingsOracle reason=audio-filter-chain audioFilter=custom-audio"
                ],
                "evidence_ref": "output:after-audio",
            },
        ]
    )

    result = await runner(
        session,
        {"output.lines_since": session.output_lines_since},
    ).run(
        one_probe_plan(
            {
                "kind": "output.field",
                "name": "audio_filter",
                "checkpoint": "before-audio",
                "source": "SettingsOracle",
                "reason": "audio-filter-chain",
                "field": "audioFilter",
                "expected": "contains:audio",
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"] == "custom-audio"
    assert probe["expected"] == "contains:audio"
    assert probe["evidence_ref"] == "output:after-audio"


@pytest.mark.asyncio
async def test_output_field_probe_fails_when_locator_has_no_match() -> None:
    session = OutputFieldProbeSession()
    session.lines_results.extend(
        [
            {"status": "PASS", "lines": []},
            {"status": "PASS", "lines": ["source=Other reason=audio-filter-chain audioFilter=on"]},
        ]
    )

    result = await runner(
        session,
        {"output.lines_since": session.output_lines_since},
    ).run(
        one_probe_plan(
            {
                "kind": "output.field",
                "name": "audio_filter",
                "checkpoint": "before-audio",
                "source": "SettingsOracle",
                "reason": "audio-filter-chain",
                "field": "audioFilter",
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "FAIL"
    assert probe["status"] == "FAIL"
    assert probe["reason"] == "output field not found"


@pytest.mark.asyncio
async def test_output_field_probe_blocks_when_execution_is_unavailable() -> None:
    session = OutputFieldProbeSession()

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "output.field",
                "name": "audio_filter",
                "checkpoint": "before-audio",
                "source": "SettingsOracle",
                "reason": "audio-filter-chain",
                "field": "audioFilter",
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "probe execution not available"
