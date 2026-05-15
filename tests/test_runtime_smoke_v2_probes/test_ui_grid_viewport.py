from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from .helpers import ProbeSmokeSession, after_probe, before_probe, one_probe_plan, runner


class GridViewportProbeSession(ProbeSmokeSession):
    def __init__(self) -> None:
        super().__init__()
        self.viewport_results: deque[dict[str, Any]] = deque()

    async def grid_viewport(
        self,
        *,
        selector: dict[str, Any],
        identity: dict[str, Any],
        rows: dict[str, Any],
        expect: dict[str, Any],
        phase: str,
        probe_name: str,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "ui.grid.viewport",
                {
                    "selector": dict(selector),
                    "identity": dict(identity),
                    "rows": dict(rows),
                    "expect": dict(expect),
                    "phase": phase,
                    "probe_name": probe_name,
                },
            )
        )
        return self.viewport_results.popleft()


def _viewport_snapshot(
    *,
    first: int,
    last: int,
    selected: list[str] | None = None,
    row_count: int = 600,
) -> dict[str, Any]:
    return {
        "first_visible_index": first,
        "last_visible_index": last,
        "visible_rows": [
            {"index": index, "identity": f"Cue {index:03d}"}
            for index in range(first, last + 1)
        ],
        "selected_rows": [
            {"index": first + offset, "identity": identity}
            for offset, identity in enumerate(selected or [])
        ],
        "row_count": row_count,
        "identity_strategy": {
            "kind": "configured_column",
            "column": "PhraseId",
        },
    }


@pytest.mark.asyncio
async def test_ui_grid_viewport_is_accepted_and_returns_identity_snapshot() -> None:
    session = GridViewportProbeSession()
    snapshot = _viewport_snapshot(first=10, last=14, selected=["Cue 011", "Cue 012"])
    snapshot["window_tree"] = {"children": [{"automation_id": "TooLarge"}]}
    session.viewport_results.append({"status": "PASS", "snapshot": snapshot})

    result = await runner(
        session,
        {"ui.grid.viewport": session.grid_viewport},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.grid.viewport",
                "name": "cue_viewport",
                "phase": "after",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert "ui.grid.viewport" in result["accepted_probe_kinds"]
    assert session.calls == [
        (
            "ui.invoke",
            {"automation_id": "ToggleSetting"},
        ),
        (
            "ui.grid.viewport",
            {
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
                "expect": {},
                "phase": "after",
                "probe_name": "cue_viewport",
            },
        ),
    ]
    assert probe["status"] == "PASS"
    assert probe["kind"] == "ui.grid.viewport"
    assert probe["value"]["first_visible_index"] == 10
    assert probe["value"]["last_visible_index"] == 14
    assert probe["value"]["selected_rows"] == [
        {"index": 10, "identity": "Cue 011"},
        {"index": 11, "identity": "Cue 012"},
    ]
    assert probe["value"]["row_count"] == 600
    assert probe["value"]["identity_strategy"] == {
        "kind": "configured_column",
        "column": "PhraseId",
    }
    assert "window_tree" not in probe["value"]


@pytest.mark.asyncio
async def test_ui_grid_viewport_blocks_when_adapter_is_missing() -> None:
    session = GridViewportProbeSession()

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "ui.grid.viewport",
                "name": "cue_viewport",
                "phase": "after",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "probe execution not available"
    assert probe["requested"] == {
        "selector": {"automation_id": "CueDataGrid"},
        "probe": "ui.grid.viewport",
    }
    assert probe["accepted"]["probe_kinds"]
    assert probe["next_step"]


@pytest.mark.asyncio
async def test_ui_grid_viewport_uses_scratch_for_changed_viewport_expectation() -> None:
    session = GridViewportProbeSession()
    session.viewport_results.extend(
        [
            {"status": "PASS", "snapshot": _viewport_snapshot(first=0, last=4)},
            {"status": "PASS", "snapshot": _viewport_snapshot(first=3, last=7)},
        ]
    )

    result = await runner(
        session,
        {"ui.grid.viewport": session.grid_viewport},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.grid.viewport",
                "name": "cue_viewport",
                "phase": "both",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
                "expect": {
                    "first_visible_index_changed": True,
                    "direction": "down",
                },
            }
        )
    )

    before = before_probe(result)
    after = after_probe(result)
    assert result["status"] == "PASS"
    assert before["value"]["first_visible_index"] == 0
    assert after["value"]["first_visible_index"] == 3
    assert after["comparison"]["first_visible_index_changed"] is True
    assert after["comparison"]["last_visible_index_changed"] is True
    assert after["comparison"]["viewport_moved"] is True
    assert after["comparison"]["direction"] == "down"
    assert after["expected"] == {
        "first_visible_index_changed": True,
        "direction": "down",
    }


@pytest.mark.asyncio
async def test_ui_grid_viewport_detects_upward_scroll_direction() -> None:
    session = GridViewportProbeSession()
    session.viewport_results.extend(
        [
            {"status": "PASS", "snapshot": _viewport_snapshot(first=10, last=14)},
            {"status": "PASS", "snapshot": _viewport_snapshot(first=6, last=10)},
        ]
    )

    result = await runner(
        session,
        {"ui.grid.viewport": session.grid_viewport},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.grid.viewport",
                "name": "cue_viewport",
                "phase": "both",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
                "expect": {
                    "viewport_moved": True,
                    "direction": "up",
                },
            }
        )
    )

    after = after_probe(result)
    assert result["status"] == "PASS"
    assert after["comparison"]["viewport_moved"] is True
    assert after["comparison"]["direction"] == "up"


@pytest.mark.asyncio
async def test_ui_grid_viewport_uses_last_visible_index_for_partial_edge_scroll() -> None:
    session = GridViewportProbeSession()
    session.viewport_results.extend(
        [
            {"status": "PASS", "snapshot": _viewport_snapshot(first=1, last=8)},
            {"status": "PASS", "snapshot": _viewport_snapshot(first=1, last=17)},
        ]
    )

    result = await runner(
        session,
        {"ui.grid.viewport": session.grid_viewport},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.grid.viewport",
                "name": "cue_viewport",
                "phase": "both",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
                "expect": {
                    "viewport_moved": True,
                    "direction": "down",
                },
            }
        )
    )

    after = after_probe(result)
    assert result["status"] == "PASS"
    assert after["comparison"]["first_visible_index_changed"] is False
    assert after["comparison"]["last_visible_index_changed"] is True
    assert after["comparison"]["viewport_moved"] is True
    assert after["comparison"]["direction"] == "down"


@pytest.mark.asyncio
async def test_ui_grid_viewport_fails_when_expected_scroll_is_unchanged() -> None:
    session = GridViewportProbeSession()
    session.viewport_results.extend(
        [
            {"status": "PASS", "snapshot": _viewport_snapshot(first=10, last=14)},
            {"status": "PASS", "snapshot": _viewport_snapshot(first=10, last=14)},
        ]
    )

    result = await runner(
        session,
        {"ui.grid.viewport": session.grid_viewport},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.grid.viewport",
                "name": "cue_viewport",
                "phase": "both",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
                "expect": {
                    "viewport_moved": True,
                    "direction": "down",
                },
            }
        )
    )

    after = after_probe(result)
    assert result["status"] == "FAIL"
    assert after["status"] == "FAIL"
    assert after["comparison"]["viewport_moved"] is False
    assert after["comparison"]["direction"] == "unchanged"


@pytest.mark.asyncio
async def test_ui_grid_viewport_fails_on_opposite_scroll_direction() -> None:
    session = GridViewportProbeSession()
    session.viewport_results.extend(
        [
            {"status": "PASS", "snapshot": _viewport_snapshot(first=10, last=14)},
            {"status": "PASS", "snapshot": _viewport_snapshot(first=5, last=9)},
        ]
    )

    result = await runner(
        session,
        {"ui.grid.viewport": session.grid_viewport},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.grid.viewport",
                "name": "cue_viewport",
                "phase": "both",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
                "expect": {
                    "viewport_moved": True,
                    "direction": "down",
                },
            }
        )
    )

    after = after_probe(result)
    assert result["status"] == "FAIL"
    assert after["comparison"]["direction"] == "up"
    assert after["expected"] == {"viewport_moved": True, "direction": "down"}


@pytest.mark.asyncio
async def test_ui_grid_viewport_compares_selected_payload_continuity() -> None:
    session = GridViewportProbeSession()
    session.viewport_results.extend(
        [
            {
                "status": "PASS",
                "snapshot": _viewport_snapshot(
                    first=0,
                    last=4,
                    selected=["Cue 001", "Cue 002"],
                ),
            },
            {
                "status": "PASS",
                "snapshot": _viewport_snapshot(
                    first=2,
                    last=6,
                    selected=["Cue 001", "Cue 003"],
                ),
            },
        ]
    )

    result = await runner(
        session,
        {"ui.grid.viewport": session.grid_viewport},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.grid.viewport",
                "name": "cue_viewport",
                "phase": "both",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
                "expect": {
                    "selected_payload_preserved": True,
                },
            }
        )
    )

    after = after_probe(result)
    assert result["status"] == "FAIL"
    assert after["comparison"]["selected_before"] == ["Cue 001", "Cue 002"]
    assert after["comparison"]["selected_after"] == ["Cue 001", "Cue 003"]
    assert after["comparison"]["selected_payload_preserved"] is False


@pytest.mark.asyncio
async def test_ui_grid_viewport_fails_duplicate_selected_payload_identities() -> None:
    session = GridViewportProbeSession()
    session.viewport_results.extend(
        [
            {
                "status": "PASS",
                "snapshot": _viewport_snapshot(
                    first=0,
                    last=4,
                    selected=["Cue 001", "Cue 001"],
                ),
            },
            {
                "status": "PASS",
                "snapshot": _viewport_snapshot(
                    first=2,
                    last=6,
                    selected=["Cue 001", "Cue 001"],
                ),
            },
        ]
    )

    result = await runner(
        session,
        {"ui.grid.viewport": session.grid_viewport},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.grid.viewport",
                "name": "cue_viewport",
                "phase": "both",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
                "expect": {
                    "selected_payload_preserved": True,
                },
            }
        )
    )

    after = after_probe(result)
    assert result["status"] == "FAIL"
    assert after["status"] == "FAIL"
    assert after["reason"] == "grid viewport expectation failed"
    assert after["comparison"]["selected_before"] == ["Cue 001", "Cue 001"]
    assert after["comparison"]["selected_after"] == ["Cue 001", "Cue 001"]
    assert after["comparison"]["selected_duplicate_identities"] == ["Cue 001"]
    assert after["comparison"]["selected_payload_preserved"] is False


@pytest.mark.asyncio
async def test_ui_grid_viewport_blocks_selected_payload_without_selected_evidence() -> None:
    session = GridViewportProbeSession()
    session.viewport_results.extend(
        [
            {"status": "PASS", "snapshot": _viewport_snapshot(first=0, last=4)},
            {"status": "PASS", "snapshot": _viewport_snapshot(first=2, last=6)},
        ]
    )

    result = await runner(
        session,
        {"ui.grid.viewport": session.grid_viewport},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.grid.viewport",
                "name": "cue_viewport",
                "phase": "both",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
                "expect": {
                    "selected_payload_preserved": True,
                },
            }
        )
    )

    after = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert after["status"] == "BLOCKED"
    assert after["reason"] == "selected row evidence unavailable"
    assert after["requested"] == {"expect": {"selected_payload_preserved": True}}
    assert after["accepted"]["selected_rows"]
    assert after["next_step"]


@pytest.mark.asyncio
async def test_ui_grid_viewport_compares_row_count_preservation() -> None:
    session = GridViewportProbeSession()
    session.viewport_results.extend(
        [
            {
                "status": "PASS",
                "snapshot": _viewport_snapshot(first=0, last=4, row_count=600),
            },
            {
                "status": "PASS",
                "snapshot": _viewport_snapshot(first=3, last=7, row_count=599),
            },
        ]
    )

    result = await runner(
        session,
        {"ui.grid.viewport": session.grid_viewport},
    ).run(
        one_probe_plan(
            {
                "kind": "ui.grid.viewport",
                "name": "cue_viewport",
                "phase": "both",
                "selector": {"automation_id": "CueDataGrid"},
                "identity": {"column": "PhraseId"},
                "rows": {"visible_only": True, "max": 5},
                "expect": {
                    "row_count_preserved": True,
                    "direction": "down",
                },
            }
        )
    )

    after = after_probe(result)
    assert result["status"] == "FAIL"
    assert after["comparison"]["before_row_count"] == 600
    assert after["comparison"]["after_row_count"] == 599
    assert after["comparison"]["row_count_preserved"] is False
