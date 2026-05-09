from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from .helpers import ProbeSmokeSession, after_probe, one_probe_plan, runner


class GridProbeSession(ProbeSmokeSession):
    def __init__(self) -> None:
        super().__init__()
        self.grid_results: deque[dict[str, Any]] = deque()

    async def grid_assert_rows(
        self,
        *,
        selector: dict[str, Any],
        rows: list[dict[str, Any]],
        columns: list[str],
    ) -> dict[str, Any]:
        self.calls.append(("ui.grid.assert_rows", dict(selector), list(rows), list(columns)))
        return self.grid_results.popleft()


@pytest.mark.asyncio
async def test_ui_grid_probe_asserts_rows_and_returns_snapshot_value() -> None:
    session = GridProbeSession()
    session.grid_results.extend([
        {
            "status": "PASS",
            "snapshot": {"visible_rows": [{"index": 0, "cells": {"Phrase": "before"}}]},
        },
        {
            "status": "PASS",
            "matched_rows": [0],
            "snapshot": {"visible_rows": [{"index": 0, "cells": {"Phrase": "after"}}]},
            "evidence_ref": "ui-grid:settings-row",
        },
    ])

    result = await runner(
        session,
        {"ui.grid.assert_rows": session.grid_assert_rows},
    ).run(one_probe_plan({
        "kind": "ui.grid",
        "name": "cue_row",
        "selector": {"automation_id": "CueGrid"},
        "rows": [{"index": 0, "contains": {"Phrase": "after"}}],
        "columns": ["Phrase"],
    }))

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"] == [{"index": 0, "cells": {"Phrase": "after"}}]
    assert probe["expected"] == [{"index": 0, "contains": {"Phrase": "after"}}]
    assert probe["evidence_ref"] == "ui-grid:settings-row"


@pytest.mark.asyncio
async def test_ui_grid_probe_blocks_when_execution_is_unavailable() -> None:
    session = GridProbeSession()

    result = await runner(session).run(one_probe_plan({
        "kind": "ui.grid",
        "name": "cue_row",
        "selector": {"automation_id": "CueGrid"},
        "rows": [{"index": 0, "contains": {"Phrase": "after"}}],
    }))

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "probe execution not available"
