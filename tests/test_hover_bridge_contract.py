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
    assert "GetProcessTopLevelWindowsStrict" in hover
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


def test_hover_root_enumeration_failure_blocks_uniqueness_proof() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")
    resolver = hover.rsplit("ResolveHoverRoot(", 1)[1].split("private static (", 1)[0]

    assert "hover root enumeration failed" in resolver
    assert "descendants = Array.Empty<AutomationElement>()" not in resolver


def test_hover_root_uniqueness_requires_complete_top_level_window_enumeration() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")
    resolver = hover.rsplit("ResolveHoverRoot(", 1)[1].split("private static (", 1)[0]
    strict_enumerator = hover.rsplit("GetProcessTopLevelWindowsStrict(", 1)[1].split(
        "private static ", 1
    )[0]

    assert "ElementCommands.GetProcessTopLevelWindows" not in resolver
    assert "GetProcessTopLevelWindowsStrict" in resolver
    assert "FindAllChildren" in strict_enumerator
    assert "NativeWindowHandle" in strict_enumerator
    assert "top-level window enumeration is incomplete" in strict_enumerator
    assert "all target-process top-level windows readable" in resolver


def test_hover_target_enumeration_failure_blocks_before_pointer_mutation() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")
    resolver = hover.rsplit("ResolveUniqueTarget(", 1)[1].split("private static (", 1)[0]

    assert resolver.count("hover target enumeration failed") == 2
    assert '"resolve_target"' in resolver
    assert '"BLOCKED"' in resolver


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


def test_hover_bridge_reserves_pointer_settle_before_mutation() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")

    move_deadline = hover.index('"move_pointer",')
    settle_reserve = hover.index("PointerSettleMs", move_deadline)
    pointer_move = hover.index("ClickCommands.MoveCursor")
    deadline_helper = hover.index("int requiredRemainingMs = 0")

    assert move_deadline < settle_reserve < pointer_move
    assert deadline_helper != -1
    assert "elapsedMs + requiredRemainingMs < timeoutMs" in hover


def test_hover_bridge_rechecks_foreground_immediately_before_pointer_mutation() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")

    focus_preflight = hover.index("focusBefore = automation.FocusedElement();")
    pointer_move = hover.index("ClickCommands.MoveCursor")
    final_foreground_read = hover.rfind("GetForegroundWindow()", focus_preflight, pointer_move)

    assert focus_preflight < final_foreground_read < pointer_move
    assert '"foreground_immediately_before_move"' in hover[final_foreground_read:pointer_move]
    assert 'pointerMutationState"] = "not_started"' in hover


def test_hover_bridge_blocks_missing_focus_and_hit_evidence() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")

    focus_before_read = hover.index("focusBefore = automation.FocusedElement();")
    focus_before_guard = hover.index("if (focusBefore is null)", focus_before_read)
    pointer_move = hover.index("ClickCommands.MoveCursor")
    assert focus_before_read < focus_before_guard < pointer_move

    hit_read = hover.index("hitElement = automation.FromPoint(actualPointer);")
    hit_guard = hover.index("if (hitElement is null)", hit_read)
    hit_relation = hover.index("hitRelation = HitRelation", hit_read)
    assert hit_read < hit_guard < hit_relation

    focus_after_read = hover.index("focusAfter = automation.FocusedElement();")
    focus_after_guard = hover.index("if (focusAfter is null)", focus_after_read)
    focus_compare = hover.index("var focusUnchanged = SafeCompare", focus_after_read)
    assert focus_after_read < focus_after_guard < focus_compare


def test_hover_bridge_validates_control_type_and_actionable_point() -> None:
    hover = BRIDGE_COMMAND.read_text(encoding="utf-8")
    resolver = hover.rsplit("ResolveUniqueTarget(", 1)[1].split("private static (", 1)[0]

    assert "catch (ArgumentException ex)" in resolver
    assert "Provide a valid controlType." in resolver
    requested_point = hover.index("var requestedPoint")
    point_bounds = hover.index("!virtualScreen.Contains(requestedPoint)")
    pointer_move = hover.index("ClickCommands.MoveCursor")
    assert requested_point < point_bounds < pointer_move
    assert "!virtualScreen.Contains(targetRect)" not in hover
