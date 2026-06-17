"""Bridge source contract tests for TextBox selection-state evidence."""

from __future__ import annotations

from pathlib import Path


def test_bridge_snapshot_selection_state_uses_text_pattern_ranges() -> None:
    source = Path("bridge/Commands/SnapshotCommands.cs").read_text(encoding="utf-8")

    assert "TextPatternRangeEndpoint.Start" in source
    assert "TextPatternRangeEndpoint.End" in source
    assert ".GetSelection()" in source
    assert "ranges is not null" in source
    assert "MoveEndpointByRange" in source
    assert "selected_text" in source
    assert "caret_index" in source
