from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_COMMAND = PROJECT_ROOT / "bridge" / "Commands" / "HoverCommands.cs"
RPC_HANDLER = PROJECT_ROOT / "bridge" / "JsonRpcHandler.cs"
ELEMENT_COMMANDS = PROJECT_ROOT / "bridge" / "Commands" / "ElementCommands.cs"


def test_hover_bridge_command_is_registered_without_changing_shared_resolver() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")
    handler = RPC_HANDLER.read_text(encoding="utf-8")
    elements = ELEMENT_COMMANDS.read_text(encoding="utf-8")

    assert '["hover"] = HoverCommands.Hover' in handler
    assert "public static JsonNode Hover(" in hover
    assert "ElementCommands.GetProcessTopLevelWindows" in hover
    assert "FindAllDescendants" in hover
    assert "ResolveHoverRoot" in hover
    assert "ResolveUniqueTarget" in hover
    assert "internal static AutomationElement ResolveSearchRoot(" in elements
    assert "window.FindFirstDescendant(cf.ByAutomationId(rootId))" in elements


def test_hover_bridge_uses_read_only_evidence_and_only_low_level_cursor_movement() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")

    for required in (
        "ClickCommands.MoveCursor",
        "GetForegroundWindow",
        "GetCursorPos",
        "GetSystemMetrics",
        "automation.FromPoint",
        "automation.FocusedElement()",
        "automation.Compare",
        ".Parent",
        "targetRootHwnd",
        "targetProcessId",
        "foregroundHwndBefore",
        "foregroundHwndAfter",
        "focusBefore",
        "focusAfter",
        "focusUnchanged",
        "targetRect",
        "requestedPoint",
        "actualPointer",
        "hitElement",
        "hitRelation",
        "underPointer",
        '["hovered"] = true',
        '["click"] = false',
        '["button"] = "none"',
    ):
        assert required in hover

    for forbidden in (
        "EnsureForeground",
        "SetForegroundWindow",
        "ShowWindow",
        "BringWindowToTop",
        "AttachThreadInput",
        "MOUSEEVENTF_LEFTDOWN",
        "MOUSEEVENTF_LEFTUP",
        ".Click(",
        ".Invoke(",
        ".Focus(",
    ):
        assert forbidden not in hover


def test_hover_bridge_checks_uniqueness_and_deadline_before_pointer_mutation() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")

    assert "rootMatches.Count" in hover
    assert "targetMatches.Count" in hover
    assert '["matchCount"]' in hover
    assert '["pointerMutationState"] = "not_started"' in hover
    assert '["pointerMutationState"] = "moved"' in hover
    assert "timeoutMs" in hover
    assert "CheckDeadline" in hover
    assert hover.index("ResolveHoverRoot") < hover.index("ClickCommands.MoveCursor")
    assert hover.index("ResolveUniqueTarget") < hover.index("ClickCommands.MoveCursor")


def test_hover_root_id_matches_automation_id_only() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")
    root_matcher = hover.split("private static bool MatchesRootIdentity", 1)[1].split(
        "private static string? HitRelation", 1
    )[0]

    assert "AutomationId" in root_matcher
    assert "Properties.Name" not in root_matcher


def test_hover_bridge_rechecks_deadline_after_postcondition_reads() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")
    postcondition_read = hover.index("var foregroundHwndAfter = GetForegroundWindow();")
    pass_result = hover.index('["status"] = "PASS"', postcondition_read)
    final_deadline = hover.find(
        'CheckDeadline(stopwatch, timeoutMs, "complete", pointerMoved)',
        postcondition_read,
        pass_result,
    )

    assert final_deadline != -1
