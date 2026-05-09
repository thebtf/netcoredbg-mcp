from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from .helpers import ProbeSmokeSession, after_probe, one_probe_plan, runner


class TracepointProbeSession(ProbeSmokeSession):
    def __init__(self) -> None:
        super().__init__()
        self.tracepoint_results: deque[dict[str, Any]] = deque()

    async def tracepoint_probe(
        self,
        *,
        file: str,
        line: int,
        expression: str,
        phase: str,
    ) -> dict[str, Any]:
        self.calls.append(("debug.tracepoint", file, line, expression, phase))
        return self.tracepoint_results.popleft()


@pytest.mark.asyncio
async def test_debug_tracepoint_probe_reports_hit_count_and_evidence_ref() -> None:
    session = TracepointProbeSession()
    session.tracepoint_results.extend([
        {"status": "PASS", "hit_count": 0, "logs": []},
        {
            "status": "PASS",
            "hit_count": 2,
            "logs": [{"value": "enabled"}],
            "evidence_ref": "tracepoint:settings-route",
        },
    ])

    result = await runner(
        session,
        {"debug.tracepoint": session.tracepoint_probe},
    ).run(one_probe_plan({
        "kind": "debug.tracepoint",
        "name": "settings_route",
        "file": "SettingsViewModel.cs",
        "line": 42,
        "expression": "Mode.SpellCheckInput",
        "expected_hit_count": 2,
    }))

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["hit_count"] == 2
    assert probe["expected"] == {"hit_count": 2}
    assert probe["evidence_ref"] == "tracepoint:settings-route"
    assert session.calls == [
        ("debug.tracepoint", "SettingsViewModel.cs", 42, "Mode.SpellCheckInput", "before"),
        ("ui.invoke", {"automation_id": "ToggleSetting"}),
        ("debug.tracepoint", "SettingsViewModel.cs", 42, "Mode.SpellCheckInput", "after"),
    ]


@pytest.mark.asyncio
async def test_debug_tracepoint_probe_blocks_when_execution_is_unavailable() -> None:
    session = TracepointProbeSession()

    result = await runner(session).run(one_probe_plan({
        "kind": "debug.tracepoint",
        "name": "settings_route",
        "file": "SettingsViewModel.cs",
        "line": 42,
        "expression": "Mode.SpellCheckInput",
    }))

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "probe execution not available"
    assert probe["accepted"]["probe_kinds"]
    assert probe["next_step"]


@pytest.mark.asyncio
async def test_debug_tracepoint_probe_rejects_invalid_numeric_fields() -> None:
    session = TracepointProbeSession()
    session.tracepoint_results.extend([
        {"status": "PASS", "hit_count": 0, "logs": []},
        {"status": "PASS", "hit_count": 0, "logs": []},
    ])

    invalid_line = await runner(
        session,
        {"debug.tracepoint": session.tracepoint_probe},
    ).run(one_probe_plan({
        "kind": "debug.tracepoint",
        "name": "bad_line",
        "file": "SettingsViewModel.cs",
        "line": "not-a-line",
        "expression": "Mode.SpellCheckInput",
    }))

    line_probe = after_probe(invalid_line)
    assert invalid_line["status"] == "FAIL"
    assert line_probe["status"] == "FAIL"
    assert line_probe["reason"] == "invalid line"
    assert session.calls == [("ui.invoke", {"automation_id": "ToggleSetting"})]

    invalid_expected = await runner(
        session,
        {"debug.tracepoint": session.tracepoint_probe},
    ).run(one_probe_plan({
        "kind": "debug.tracepoint",
        "name": "bad_expected",
        "file": "SettingsViewModel.cs",
        "line": 42,
        "expression": "Mode.SpellCheckInput",
        "expected_hit_count": "not-a-count",
    }))

    expected_probe = after_probe(invalid_expected)
    assert invalid_expected["status"] == "FAIL"
    assert expected_probe["status"] == "FAIL"
    assert expected_probe["reason"] == "invalid expected_hit_count"
