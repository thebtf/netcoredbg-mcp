from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.output_assertions import OutputAssertionService
from netcoredbg_mcp.session.state import OutputEntry

from .helpers import ProbeSmokeSession, after_probe, one_probe_plan, runner


class OutputSinceProbeSession(ProbeSmokeSession):
    def __init__(self) -> None:
        super().__init__()
        self.assertion_results: deque[dict[str, Any]] = deque()

    async def output_assert_since(
        self,
        *,
        checkpoint: str,
        required: list[str],
        forbidden: list[str],
        regex: bool,
        max_matches: int,
    ) -> dict[str, Any]:
        self.calls.append((
            "output_assert_since",
            checkpoint,
            tuple(required),
            tuple(forbidden),
            regex,
            max_matches,
        ))
        return self.assertion_results.popleft()


@pytest.mark.asyncio
async def test_output_since_probe_wraps_output_assertion() -> None:
    session = OutputSinceProbeSession()
    session.assertion_results.extend([
        {"status": "PASS", "matches": [], "missing_required": [], "forbidden_matches": []},
        {
            "status": "PASS",
            "matches": [{"text": "SettingsOracle reason=spellcheck"}],
            "missing_required": [],
            "forbidden_matches": [],
            "evidence_refs": [{"ref": "output:after-spellcheck"}],
        },
    ])

    result = await runner(
        session,
        {"output_assert_since": session.output_assert_since},
    ).run(one_probe_plan({
        "kind": "output.since",
        "name": "spellcheck_output",
        "checkpoint": "before-spellcheck",
        "required": ["SettingsOracle"],
        "forbidden": ["exception"],
        "regex": False,
        "max_matches": 5,
    }))

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["matched_line_count"] == 1
    assert probe["evidence_ref"] == "output:after-spellcheck"


@pytest.mark.asyncio
async def test_output_since_probe_reuses_session_output_assertion_service() -> None:
    session = OutputSinceProbeSession()
    session.state = SimpleNamespace(
        output_buffer=[OutputEntry("before\n")],
        output_sequence=0,
        output_trimmed_before=0,
    )
    OutputAssertionService(session).create_checkpoint("before-spellcheck")
    session.state.output_buffer.append(OutputEntry("SettingsOracle reason=spellcheck\n"))

    result = await runner(session).run(one_probe_plan({
        "kind": "output.since",
        "name": "spellcheck_output",
        "checkpoint": "before-spellcheck",
        "required": ["SettingsOracle"],
    }))

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["matched_line_count"] == 1


@pytest.mark.asyncio
async def test_output_since_probe_blocks_when_execution_is_unavailable() -> None:
    session = OutputSinceProbeSession()

    result = await runner(session).run(one_probe_plan({
        "kind": "output.since",
        "name": "spellcheck_output",
        "checkpoint": "before-spellcheck",
        "required": ["SettingsOracle"],
    }))

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "probe execution not available"
