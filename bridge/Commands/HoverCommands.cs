using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static partial class HoverCommands
{
    [LibraryImport("user32.dll")]
    private static partial IntPtr GetForegroundWindow();

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool GetCursorPos(out NativePoint point);

    [LibraryImport("user32.dll")]
    private static partial int GetSystemMetrics(int index);

    [StructLayout(LayoutKind.Sequential)]
    private struct NativePoint
    {
        public int X;
        public int Y;
    }

    private const int SmXVirtualScreen = 76;
    private const int SmYVirtualScreen = 77;
    private const int SmCxVirtualScreen = 78;
    private const int SmCyVirtualScreen = 79;
    private const int PointerSettleMs = 50;
    private const int MaxAncestorDepth = 64;
    private const string Blocked = "BLOCKED";
    private const string ResolveRoot = "resolve_root";
    private const string ResolveTarget = "resolve_target";
    private const string ValidateTarget = "validate_target";
    private const string TargetProcessIdKey = "targetProcessId";
    private const string TargetRootHwndKey = "targetRootHwnd";
    private const string TargetRectKey = "targetRect";
    private const string ForegroundVerifiedKey = "foregroundVerified";
    private const string FocusBeforeKey = "focusBefore";
    private const string MatchCountKey = "matchCount";
    private const string PointerMutationStateKey = "pointerMutationState";
    private const string Moved = "moved";
    private const string RootAutomationIdKey = "rootAutomationId";
    private const string AutomationIdKey = "automationId";
    private const string XPathKey = "xpath";
    private const string ControlTypeKey = "controlType";
    private const string ForegroundHwndBeforeKey = "foregroundHwndBefore";
    private const string RequestedPointKey = "requestedPoint";

    public static JsonNode Hover(
        JsonNode? @params,
        UIA3Automation automation,
        AutomationElement? mainWindow)
    {
        if (mainWindow is null)
        {
            throw new InvalidOperationException("Not connected. Call 'connect' first.");
        }

        var timeoutMs = ReadTimeoutMs(@params);
        var stopwatch = Stopwatch.StartNew();
        var pointerMoved = false;

        if (JsonRpcHandler.Stealth)
        {
            return NotStartedFailure(
                "selector-scoped pointer hover is unavailable in stealth mode",
                "stealth",
                (timeoutMs, stopwatch),
                requested: RequestedSelector(@params),
                accepted: new JsonObject
                {
                    ["stealth"] = false,
                    ["capability"] = "foreground physical pointer hover",
                },
                nextStep: "Reconnect without stealth mode and establish the exact target foreground window.");
        }

        var (target, targetWindow, resolvedSelector, selectionFailure) = ResolveHoverSelection(
            mainWindow, @params, automation, timeoutMs, stopwatch);
        if (selectionFailure is not null)
        {
            return selectionFailure;
        }

        var targetRootHwnd = SafeWindowHandle(targetWindow!);
        var targetProcessId = SafeProcessId(targetWindow!);
        if (targetRootHwnd == IntPtr.Zero || targetProcessId <= 0 ||
            targetProcessId != JsonRpcHandler.ProcessId ||
            SafeProcessId(target!) != JsonRpcHandler.ProcessId)
        {
            return NotStartedFailure(
                "hover target does not belong to a usable top-level window in the attached process",
                ValidateTarget,
                (timeoutMs, stopwatch),
                requested: RequestedSelector(@params),
                accepted: new JsonObject
                {
                    [TargetProcessIdKey] = JsonRpcHandler.ProcessId,
                    [TargetRootHwndKey] = "non-zero HWND owned by the attached process",
                },
                nextStep: "Reconnect to the target process and resolve a target inside one of its top-level windows.",
                extra: new JsonObject
                {
                    [TargetRootHwndKey] = targetRootHwnd.ToInt64(),
                    [TargetProcessIdKey] = targetProcessId,
                });
        }

        Rectangle targetRect;
        bool isOffscreen;
        try
        {
            targetRect = target!.BoundingRectangle;
            isOffscreen = target.IsOffscreen;
        }
        catch (Exception ex)
        {
            return NotStartedFailure(
                $"hover target bounds are unavailable: {ex.Message}",
                ValidateTarget,
                (timeoutMs, stopwatch),
                requested: RequestedSelector(@params),
                accepted: new JsonObject { [TargetRectKey] = "positive on-screen rectangle" },
                nextStep: "Ensure the target is realized, visible, and inside the virtual desktop.");
        }

        var requestedPoint = new Point(
            targetRect.Left + (targetRect.Width / 2),
            targetRect.Top + (targetRect.Height / 2));
        var virtualScreen = VirtualScreenBounds();
        if (targetRect.Width <= 0 || targetRect.Height <= 0 || isOffscreen ||
            virtualScreen.Width <= 0 || virtualScreen.Height <= 0 ||
            !virtualScreen.Contains(requestedPoint))
        {
            return NotStartedFailure(
                "hover target is off-screen or its actionable point is outside virtual-screen bounds",
                ValidateTarget,
                (timeoutMs, stopwatch),
                requested: RequestedSelector(@params),
                accepted: new JsonObject { [TargetRectKey] = "positive rectangle with an actionable point inside the virtual screen" },
                nextStep: "Scroll or move the target so its center point is on-screen before hovering.",
                extra: new JsonObject
                {
                    [TargetRectKey] = RectJson(targetRect),
                    ["virtualScreen"] = RectJson(virtualScreen),
                    ["isOffscreen"] = isOffscreen,
                });
        }
        var foregroundHwndBefore = GetForegroundWindow();
        if (foregroundHwndBefore != targetRootHwnd)
        {
            return NotStartedFailure(
                "exact target-root foreground prerequisite is not satisfied",
                "foreground_before",
                (timeoutMs, stopwatch),
                requested: RequestedSelector(@params),
                accepted: new JsonObject { ["foregroundHwnd"] = targetRootHwnd.ToInt64() },
                nextStep: "Use ui.input.ensure_target or harness setup to foreground the exact target root, then retry.",
                extra: new JsonObject
                {
                    [TargetRootHwndKey] = targetRootHwnd.ToInt64(),
                    [TargetProcessIdKey] = targetProcessId,
                    [ForegroundHwndBeforeKey] = foregroundHwndBefore.ToInt64(),
                    [ForegroundVerifiedKey] = false,
                    [TargetRectKey] = RectJson(targetRect),
                    [RequestedPointKey] = PointJson(requestedPoint),
                });
        }

        AutomationElement focusBefore;
        try
        {
            focusBefore = automation.FocusedElement();
            if (focusBefore is null)
            {
                return NotStartedFailure(
                    "focused-element evidence is unavailable before hover",
                    "focus_before",
                    (timeoutMs, stopwatch),
                    requested: RequestedSelector(@params),
                    accepted: new JsonObject { [FocusBeforeKey] = "non-null focused AutomationElement" },
                    nextStep: "Establish keyboard focus inside the target window and retry.");
            }
        }
        catch (Exception ex)
        {
            return NotStartedFailure(
                $"focused-element evidence is unavailable before hover: {ex.Message}",
                "focus_before",
                (timeoutMs, stopwatch),
                requested: RequestedSelector(@params),
                accepted: new JsonObject { [FocusBeforeKey] = "readable focused AutomationElement" },
                nextStep: "Establish keyboard focus inside the target window and retry.");
        }

        var deadlineFailure = CheckDeadline(
            stopwatch,
            timeoutMs,
            "move_pointer",
            pointerMoved,
            PointerSettleMs);
        if (deadlineFailure is not null)
        {
            return deadlineFailure;
        }

        var foregroundHwndImmediatelyBeforeMove = GetForegroundWindow();
        if (foregroundHwndImmediatelyBeforeMove != targetRootHwnd)
        {
            return NotStartedFailure(
                "exact target-root foreground changed before pointer movement",
                "foreground_immediately_before_move",
                (timeoutMs, stopwatch),
                requested: RequestedSelector(@params),
                accepted: new JsonObject { ["foregroundHwnd"] = targetRootHwnd.ToInt64() },
                nextStep: "Restore the exact target root to the foreground and retry.",
                extra: new JsonObject
                {
                    [TargetRootHwndKey] = targetRootHwnd.ToInt64(),
                    [TargetProcessIdKey] = targetProcessId,
                    [ForegroundHwndBeforeKey] = foregroundHwndBefore.ToInt64(),
                    ["foregroundHwndImmediatelyBeforeMove"] = foregroundHwndImmediatelyBeforeMove.ToInt64(),
                    [ForegroundVerifiedKey] = false,
                    [TargetRectKey] = RectJson(targetRect),
                    [RequestedPointKey] = PointJson(requestedPoint),
                });
        }

        var evidence = (
            RootHwnd: targetRootHwnd,
            ProcessId: targetProcessId,
            ForegroundBefore: foregroundHwndBefore,
            Rect: targetRect,
            RequestedPoint: requestedPoint);

        ClickCommands.MoveCursor(requestedPoint.X, requestedPoint.Y);
        Thread.Sleep(PointerSettleMs);
        return VerifyHoverAfterMove(
            automation,
            stopwatch,
            timeoutMs,
            (target!, resolvedSelector!, focusBefore, foregroundHwndImmediatelyBeforeMove),
            evidence);

    }

    private static (
        AutomationElement? Target,
        AutomationElement? TargetWindow,
        JsonObject? ResolvedSelector,
        JsonObject? Failure) ResolveHoverSelection(
            AutomationElement mainWindow,
            JsonNode? @params,
            UIA3Automation automation,
            int timeoutMs,
            Stopwatch stopwatch)
    {
        var deadlineFailure = CheckDeadline(stopwatch, timeoutMs, ResolveRoot, pointerMoved: false);
        if (deadlineFailure is not null)
        {
            return (null, null, null, deadlineFailure);
        }

        var (searchRoot, targetWindow, rootFailure) = ResolveHoverRoot(
            mainWindow, @params, automation, timeoutMs, stopwatch);
        if (rootFailure is not null)
        {
            return (null, null, null, rootFailure);
        }

        deadlineFailure = CheckDeadline(stopwatch, timeoutMs, ResolveTarget, pointerMoved: false);
        if (deadlineFailure is not null)
        {
            return (null, null, null, deadlineFailure);
        }

        var (target, resolvedSelector, targetFailure) = ResolveUniqueTarget(
            searchRoot!, @params, automation, timeoutMs, stopwatch);
        if (targetFailure is not null)
        {
            return (null, null, null, targetFailure);
        }

        deadlineFailure = CheckDeadline(stopwatch, timeoutMs, ValidateTarget, pointerMoved: false);
        return deadlineFailure is null
            ? (target, targetWindow, resolvedSelector, null)
            : (null, null, null, deadlineFailure);
    }

    private static JsonNode VerifyHoverAfterMove(
        UIA3Automation automation,
        Stopwatch stopwatch,
        int timeoutMs,
        (AutomationElement Target, JsonObject ResolvedSelector, AutomationElement FocusBefore, IntPtr ForegroundBeforeMove) resolved,
        (IntPtr RootHwnd, int ProcessId, IntPtr ForegroundBefore, Rectangle Rect, Point RequestedPoint) evidence)
    {
        var (target, resolvedSelector, focusBefore, foregroundHwndImmediatelyBeforeMove) = resolved;
        var (targetRootHwnd, targetProcessId, foregroundHwndBefore, targetRect, requestedPoint) = evidence;
        var pointerMoved = true;
        JsonObject? deadlineFailure;
        deadlineFailure = CheckDeadline(stopwatch, timeoutMs, "pointer_readback", pointerMoved);
        if (deadlineFailure is not null)
        {
            return deadlineFailure;
        }

        if (!GetCursorPos(out var nativePoint))
        {
            return MovedFailure(
                $"GetCursorPos failed with Win32 error {Marshal.GetLastWin32Error()}",
                "pointer_readback",
                timeoutMs,
                stopwatch,
                evidence);
        }

        var actualPointer = new Point(nativePoint.X, nativePoint.Y);
        if (!targetRect.Contains(actualPointer))
        {
            return MovedFailure(
                "actual pointer is outside the resolved hover target",
                "pointer_readback",
                timeoutMs,
                stopwatch,
                evidence,
                actualPointer);
        }

        var (hitElement, hitRelation, hitFailure) = VerifyHitTest(
            target, actualPointer, automation, timeoutMs, stopwatch, evidence);
        if (hitFailure is not null)
        {
            return hitFailure;
        }

        deadlineFailure = CheckDeadline(stopwatch, timeoutMs, "postconditions", pointerMoved);
        if (deadlineFailure is not null)
        {
            return deadlineFailure;
        }

        AutomationElement focusAfter;
        try
        {
            focusAfter = automation.FocusedElement();
            if (focusAfter is null)
            {
                return MovedFailure(
                    "focused-element evidence is unavailable after hover",
                    "focus_after",
                    timeoutMs,
                    stopwatch,
                    evidence,
                    actualPointer);
            }
        }
        catch (Exception ex)
        {
            return MovedFailure(
                $"focused-element evidence is unavailable after hover: {ex.Message}",
                "focus_after",
                timeoutMs,
                stopwatch,
                evidence,
                actualPointer);
        }

        var focusUnchanged = SafeCompare(focusBefore, focusAfter, automation);
        var foregroundHwndAfter = GetForegroundWindow();
        var foregroundVerified = foregroundHwndBefore == targetRootHwnd &&
            foregroundHwndImmediatelyBeforeMove == targetRootHwnd &&
            foregroundHwndAfter == targetRootHwnd;

        if (!foregroundVerified || !focusUnchanged)
        {
            return MovedFailure(
                !foregroundVerified
                    ? "target-root foreground changed during hover"
                    : "keyboard focus changed during hover",
                !foregroundVerified ? "foreground_after" : "focus_after",
                timeoutMs,
                stopwatch,
                evidence,
                actualPointer,
                new JsonObject
                {
                    ["foregroundHwndAfter"] = foregroundHwndAfter.ToInt64(),
                    [ForegroundVerifiedKey] = foregroundVerified,
                    [FocusBeforeKey] = ElementCommands.BuildElementInfo(focusBefore, includePatterns: false),
                    ["focusAfter"] = ElementCommands.BuildElementInfo(focusAfter, includePatterns: false),
                    ["focusUnchanged"] = focusUnchanged,
                });
        }

        deadlineFailure = CheckDeadline(stopwatch, timeoutMs, "complete", pointerMoved);
        if (deadlineFailure is not null)
        {
            return deadlineFailure;
        }

        stopwatch.Stop();
        return new JsonObject
        {
            ["status"] = "PASS",
            ["phase"] = "complete",
            ["resolvedSelector"] = resolvedSelector,
            ["target"] = ElementCommands.BuildElementInfo(target, includePatterns: false),
            [MatchCountKey] = 1,
            [TargetRootHwndKey] = targetRootHwnd.ToInt64(),
            [TargetProcessIdKey] = targetProcessId,
            [ForegroundHwndBeforeKey] = foregroundHwndBefore.ToInt64(),
            ["foregroundHwndImmediatelyBeforeMove"] = foregroundHwndImmediatelyBeforeMove.ToInt64(),
            ["foregroundHwndAfter"] = foregroundHwndAfter.ToInt64(),
            [ForegroundVerifiedKey] = true,
            [FocusBeforeKey] = ElementCommands.BuildElementInfo(focusBefore, includePatterns: false),
            ["focusAfter"] = ElementCommands.BuildElementInfo(focusAfter, includePatterns: false),
            ["focusUnchanged"] = true,
            [TargetRectKey] = RectJson(targetRect),
            [RequestedPointKey] = PointJson(requestedPoint),
            ["actualPointer"] = PointJson(actualPointer),
            ["hitElement"] = ElementCommands.BuildElementInfo(hitElement!, includePatterns: false),
            ["hitRelation"] = hitRelation,
            ["underPointer"] = true,
            ["hovered"] = true,
            ["click"] = false,
            ["button"] = "none",
            ["timeoutMs"] = timeoutMs,
            ["elapsedMs"] = stopwatch.ElapsedMilliseconds,
            [PointerMutationStateKey] = Moved,
        };
    }

    private static (AutomationElement? Hit, string? Relation, JsonObject? Failure) VerifyHitTest(
        AutomationElement target,
        Point actualPointer,
        UIA3Automation automation,
        int timeoutMs,
        Stopwatch stopwatch,
        (IntPtr RootHwnd, int ProcessId, IntPtr ForegroundBefore, Rectangle Rect, Point RequestedPoint) evidence)
    {
        AutomationElement? hitElement;
        string? hitRelation;
        try
        {
            hitElement = automation.FromPoint(actualPointer);
            if (hitElement is null)
            {
                return (null, null, MovedFailure(
                    "UIA hit-test returned no element after pointer movement",
                    "hit_test", timeoutMs, stopwatch, evidence, actualPointer));
            }
            hitRelation = HitRelation(target, hitElement, automation);
        }
        catch (Exception ex)
        {
            return (null, null, MovedFailure(
                $"UIA hit-test failed after pointer movement: {ex.Message}",
                "hit_test", timeoutMs, stopwatch, evidence, actualPointer));
        }

        if (hitRelation is null)
        {
            return (null, null, MovedFailure(
                "UIA hit-test does not resolve to the hover target or one of its descendants",
                "hit_test", timeoutMs, stopwatch, evidence, actualPointer,
                new JsonObject
                {
                    ["hitElement"] = ElementCommands.BuildElementInfo(hitElement, includePatterns: false),
                    ["hitRelation"] = "unrelated",
                    ["underPointer"] = false,
                }));
        }
        return (hitElement, hitRelation, null);
    }

    private static (
        AutomationElement? SearchRoot,
        AutomationElement? TargetWindow,
        JsonObject? Failure) ResolveHoverRoot(
            AutomationElement mainWindow,
            JsonNode? @params,
            UIA3Automation automation,
            int timeoutMs,
            Stopwatch stopwatch)
    {
        var rootId = ParamString(@params, RootAutomationIdKey);
        if (string.IsNullOrWhiteSpace(rootId))
        {
            return (mainWindow, mainWindow, null);
        }

        var (topLevelWindows, topLevelEnumerationFailure) =
            GetProcessTopLevelWindowsStrict(mainWindow, automation);
        if (topLevelEnumerationFailure is not null)
        {
            return (
                null,
                null,
                NotStartedFailure(
                    topLevelEnumerationFailure,
                    ResolveRoot,
                    (timeoutMs, stopwatch),
                    requested: new JsonObject { [RootAutomationIdKey] = rootId },
                    accepted: new JsonObject { ["rootEnumeration"] = "all target-process top-level windows readable" },
                    nextStep: "Reconnect to a responsive target process and retry root uniqueness validation."));
        }

        var rootMatches = new List<(AutomationElement Element, AutomationElement Window)>();
        var condition = new ConditionFactory(automation.PropertyLibrary).ByAutomationId(rootId);
        foreach (var window in topLevelWindows!)
        {
            AutomationElement[] descendants;
            try
            {
                if (MatchesRootIdentity(window, rootId))
                {
                    AddUniqueRoot(rootMatches, window, window, automation);
                }
                descendants = window.FindAllDescendants(condition);
            }
            catch (Exception ex)
            {
                return (
                    null,
                    null,
                    NotStartedFailure(
                        $"hover root enumeration failed: {ex.Message}",
                        ResolveRoot,
                        (timeoutMs, stopwatch),
                        requested: new JsonObject { [RootAutomationIdKey] = rootId },
                        accepted: new JsonObject { ["rootEnumeration"] = "all target-process top-level windows readable" },
                        nextStep: "Reconnect to a responsive target process and retry root uniqueness validation."));
            }

            foreach (var descendant in descendants)
            {
                AddUniqueRoot(rootMatches, descendant, window, automation);
            }
        }

        if (rootMatches.Count != 1)
        {
            return (
                null,
                null,
                NotStartedFailure(
                    rootMatches.Count == 0
                        ? "hover root selector did not match any element"
                        : "hover root selector is ambiguous",
                    ResolveRoot,
                    (timeoutMs, stopwatch),
                    requested: new JsonObject { [RootAutomationIdKey] = rootId },
                    accepted: new JsonObject { [MatchCountKey] = 1 },
                    nextStep: "Use a root_id that resolves to exactly one element across the target process.",
                    extra: new JsonObject { [MatchCountKey] = rootMatches.Count }));
        }

        return (rootMatches[0].Element, rootMatches[0].Window, null);
    }

    private static (
        List<AutomationElement>? Windows,
        string? Failure) GetProcessTopLevelWindowsStrict(
            AutomationElement mainWindow,
            UIA3Automation automation)
    {
        var processId = JsonRpcHandler.ProcessId;
        if (processId <= 0)
        {
            return (
                null,
                "top-level window enumeration is incomplete: attached process id is unavailable");
        }

        AutomationElement[] siblings;
        try
        {
            var desktop = automation.GetDesktop();
            var condition = new ConditionFactory(automation.PropertyLibrary).ByProcessId(processId);
            siblings = desktop.FindAllChildren(condition);
        }
        catch (Exception ex)
        {
            return (
                null,
                $"top-level window enumeration is incomplete: {ex.Message}");
        }

        if (siblings.Length == 0)
        {
            return (
                null,
                "top-level window enumeration is incomplete: no attached-process windows were returned");
        }

        var windows = new List<AutomationElement>(siblings.Length);
        var seenHandles = new HashSet<IntPtr>();
        foreach (var sibling in siblings)
        {
            IntPtr handle;
            int siblingProcessId;
            try
            {
                handle = sibling.Properties.NativeWindowHandle.ValueOrDefault;
                siblingProcessId = sibling.Properties.ProcessId.ValueOrDefault;
            }
            catch (Exception ex)
            {
                return (
                    null,
                    $"top-level window enumeration is incomplete: window identity is unreadable ({ex.Message})");
            }

            if (handle == IntPtr.Zero || siblingProcessId != processId)
            {
                return (
                    null,
                    "top-level window enumeration is incomplete: every returned window must expose a non-zero HWND owned by the attached process");
            }

            if (seenHandles.Add(handle))
            {
                windows.Add(sibling);
            }
        }

        var mainWindowHandle = SafeWindowHandle(mainWindow);
        if (mainWindowHandle == IntPtr.Zero || !seenHandles.Contains(mainWindowHandle))
        {
            return (
                null,
                "top-level window enumeration is incomplete: the connected root window was not present in the process-wide result");
        }

        return (windows, null);
    }

    private static (
        AutomationElement? Target,
        JsonObject? ResolvedSelector,
        JsonObject? Failure) ResolveUniqueTarget(
            AutomationElement searchRoot,
            JsonNode? @params,
            UIA3Automation automation,
            int timeoutMs,
            Stopwatch stopwatch)
    {
        var cf = new ConditionFactory(automation.PropertyLibrary);
        var automationId = ParamString(@params, AutomationIdKey);
        var xpath = ParamString(@params, XPathKey);
        var name = ParamString(@params, "name");
        var controlType = ParamString(@params, ControlTypeKey);
        JsonObject? lastSelector = null;
        List<AutomationElement> targetMatches;

        if (!string.IsNullOrWhiteSpace(automationId))
        {
            lastSelector = new JsonObject
            {
                ["criterion"] = AutomationIdKey,
                [AutomationIdKey] = automationId,
            };
            try
            {
                targetMatches = searchRoot.FindAllDescendants(cf.ByAutomationId(automationId)).ToList();
            }
            catch (Exception ex)
            {
                return (
                    null,
                    null,
                    NotStartedFailure(
                        $"hover target enumeration failed: {ex.Message}",
                        ResolveTarget,
                        (timeoutMs, stopwatch),
                        requested: RequestedSelector(@params),
                        accepted: new JsonObject { ["targetEnumeration"] = "scoped descendants readable" },
                        nextStep: "Reconnect to a responsive target process and retry target uniqueness validation."));
            }
            if (targetMatches.Count > 0)
            {
                return UniqueTargetResult(targetMatches, lastSelector, @params, timeoutMs, stopwatch);
            }
        }

        if (!string.IsNullOrWhiteSpace(xpath))
        {
            lastSelector = new JsonObject
            {
                ["criterion"] = XPathKey,
                [XPathKey] = xpath,
            };
            try
            {
                targetMatches = searchRoot.FindAllByXPath(xpath).ToList();
            }
            catch (Exception ex)
            {
                return (
                    null,
                    null,
                    NotStartedFailure(
                        $"hover XPath selector is invalid or unavailable: {ex.Message}",
                        ResolveTarget,
                        (timeoutMs, stopwatch),
                        requested: RequestedSelector(@params),
                        accepted: new JsonObject { [XPathKey] = "valid FlaUI XPath" },
                        nextStep: "Correct the XPath or use automation_id/name/control_type."));
            }
            if (targetMatches.Count > 0)
            {
                return UniqueTargetResult(targetMatches, lastSelector, @params, timeoutMs, stopwatch);
            }
        }

        if (!string.IsNullOrWhiteSpace(name) || !string.IsNullOrWhiteSpace(controlType))
        {
            lastSelector = new JsonObject
            {
                ["criterion"] = "name+controlType",
                ["name"] = name,
                [ControlTypeKey] = controlType,
            };
            var (nameMatches, nameFailure) = ResolveNameMatches(searchRoot, @params, cf, timeoutMs, stopwatch);
            if (nameFailure is not null)
            {
                return (null, null, nameFailure);
            }
            if (nameMatches!.Count > 0)
            {
                return UniqueTargetResult(nameMatches, lastSelector, @params, timeoutMs, stopwatch);
            }
        }

        return (
            null,
            null,
            NotStartedFailure(
                lastSelector is null
                    ? "hover requires a target selector"
                    : "hover target selector did not match any element",
                ResolveTarget,
                (timeoutMs, stopwatch),
                requested: RequestedSelector(@params),
                accepted: new JsonObject
                {
                    ["selector"] = "automationId, xpath, or name/controlType with exactly one match",
                    [MatchCountKey] = 1,
                },
                nextStep: "Inspect the scoped tree and provide a selector that resolves to exactly one target.",
                extra: new JsonObject
                {
                    ["resolvedSelector"] = lastSelector,
                    [MatchCountKey] = 0,
                }));
    }

    private static (List<AutomationElement>? Matches, JsonObject? Failure) ResolveNameMatches(
        AutomationElement searchRoot,
        JsonNode? @params,
        ConditionFactory cf,
        int timeoutMs,
        Stopwatch stopwatch)
    {
        var name = ParamString(@params, "name");
        var controlType = ParamString(@params, ControlTypeKey);
        var conditions = new List<ConditionBase>();
        if (!string.IsNullOrWhiteSpace(name))
        {
            conditions.Add(cf.ByName(name));
        }
        if (!string.IsNullOrWhiteSpace(controlType))
        {
            try
            {
                conditions.Add(cf.ByControlType(ParseControlType(controlType)));
            }
            catch (ArgumentException ex)
            {
                return (null, NotStartedFailure(
                    ex.Message,
                    ResolveTarget,
                    (timeoutMs, stopwatch),
                    requested: RequestedSelector(@params),
                    accepted: new JsonObject { [ControlTypeKey] = "valid FlaUI ControlType" },
                    nextStep: "Provide a valid controlType."));
            }
        }
        var condition = conditions.Count == 1
            ? conditions[0]
            : new AndCondition(conditions.ToArray());
        try
        {
            return (searchRoot.FindAllDescendants(condition).ToList(), null);
        }
        catch (Exception ex)
        {
            return (null, NotStartedFailure(
                $"hover target enumeration failed: {ex.Message}",
                ResolveTarget,
                (timeoutMs, stopwatch),
                requested: RequestedSelector(@params),
                accepted: new JsonObject { ["targetEnumeration"] = "scoped descendants readable" },
                nextStep: "Reconnect to a responsive target process and retry target uniqueness validation."));
        }
    }

    private static (
        AutomationElement? Target,
        JsonObject? ResolvedSelector,
        JsonObject? Failure) UniqueTargetResult(
            List<AutomationElement> targetMatches,
            JsonObject resolvedSelector,
            JsonNode? @params,
            int timeoutMs,
            Stopwatch stopwatch)
    {
        if (targetMatches.Count == 1)
        {
            return (targetMatches[0], resolvedSelector, null);
        }

        return (
            null,
            null,
            NotStartedFailure(
                "hover target selector is ambiguous",
                ResolveTarget,
                (timeoutMs, stopwatch),
                requested: RequestedSelector(@params),
                accepted: new JsonObject { [MatchCountKey] = 1 },
                nextStep: "Add a unique root_id or stronger selector so exactly one target matches.",
                extra: new JsonObject
                {
                    ["resolvedSelector"] = resolvedSelector,
                    [MatchCountKey] = targetMatches.Count,
                }));
    }

    private static JsonObject? CheckDeadline(
        Stopwatch stopwatch,
        int timeoutMs,
        string phase,
        bool pointerMoved,
        int requiredRemainingMs = 0)
    {
        var elapsedMs = stopwatch.ElapsedMilliseconds;
        if (elapsedMs + requiredRemainingMs < timeoutMs)
        {
            return null;
        }

        var result = new JsonObject
        {
            ["status"] = Blocked,
            ["reason"] = requiredRemainingMs > 0
                ? "hover deadline cannot accommodate required pre-mutation work"
                : "hover deadline exceeded",
            ["phase"] = phase,
            ["timeoutMs"] = timeoutMs,
            ["elapsedMs"] = elapsedMs,
            ["requiredRemainingMs"] = requiredRemainingMs,
            ["remainingMs"] = Math.Max(0, timeoutMs - elapsedMs),
            ["requested"] = new JsonObject { ["timeoutMs"] = timeoutMs },
            ["accepted"] = new JsonObject
            {
                ["deadline"] = $"elapsedMs + requiredRemainingMs must be less than {timeoutMs}",
            },
            ["next_step"] = "Use a responsive foreground target or increase timeout_ms within 1..30000.",
        };
        MarkMutationState(result, pointerMoved);
        return result;
    }

    private static JsonObject NotStartedFailure(
        string reason,
        string phase,
        (int TimeoutMs, Stopwatch Stopwatch) timing,
        JsonObject requested,
        JsonObject accepted,
        string nextStep,
        JsonObject? extra = null)
    {
        var result = Failure(Blocked, reason, phase, timing, requested, accepted, nextStep);
        result[PointerMutationStateKey] = "not_started";
        Merge(result, extra);
        return result;
    }

    private static JsonObject MovedFailure(
        string reason,
        string phase,
        int timeoutMs,
        Stopwatch stopwatch,
        (IntPtr RootHwnd, int ProcessId, IntPtr ForegroundBefore, Rectangle Rect, Point RequestedPoint) evidence,
        Point? actualPointer = null,
        JsonObject? extra = null)
    {
        var result = Failure(
            "FAIL",
            reason,
            phase,
            (timeoutMs, stopwatch),
            new JsonObject { [TargetRootHwndKey] = evidence.RootHwnd.ToInt64() },
            new JsonObject { ["hoverEvidence"] = "complete and internally consistent" },
            "Re-establish target foreground/focus/visibility, inspect occlusion, and retry.");
        result[PointerMutationStateKey] = Moved;
        result[TargetRootHwndKey] = evidence.RootHwnd.ToInt64();
        result[TargetProcessIdKey] = evidence.ProcessId;
        result[ForegroundHwndBeforeKey] = evidence.ForegroundBefore.ToInt64();
        result[TargetRectKey] = RectJson(evidence.Rect);
        result[RequestedPointKey] = PointJson(evidence.RequestedPoint);
        if (actualPointer is not null)
        {
            result["actualPointer"] = PointJson(actualPointer.Value);
        }
        Merge(result, extra);
        return result;
    }

    private static JsonObject Failure(
        string status,
        string reason,
        string phase,
        (int TimeoutMs, Stopwatch Stopwatch) timing,
        JsonObject requested,
        JsonObject accepted,
        string nextStep)
    {
        return new JsonObject
        {
            ["status"] = status,
            ["reason"] = reason,
            ["phase"] = phase,
            ["timeoutMs"] = timing.TimeoutMs,
            ["elapsedMs"] = timing.Stopwatch.ElapsedMilliseconds,
            ["requested"] = requested,
            ["accepted"] = accepted,
            ["next_step"] = nextStep,
        };
    }

    private static void MarkMutationState(JsonObject result, bool pointerMoved)
    {
        if (pointerMoved)
        {
            result[PointerMutationStateKey] = Moved;
            return;
        }
        result[PointerMutationStateKey] = "not_started";
    }

    private static void Merge(JsonObject target, JsonObject? extra)
    {
        if (extra is null)
        {
            return;
        }
        foreach (var pair in extra)
        {
            target[pair.Key] = pair.Value?.DeepClone();
        }
    }

    private static void AddUniqueRoot(
        List<(AutomationElement Element, AutomationElement Window)> roots,
        AutomationElement candidate,
        AutomationElement window,
        UIA3Automation automation)
    {
        if (roots.Any(existing => SafeCompare(existing.Element, candidate, automation)))
        {
            return;
        }
        roots.Add((candidate, window));
    }

    private static bool MatchesRootIdentity(AutomationElement element, string rootId)
    {
        return element.Properties.AutomationId.IsSupported && element.AutomationId == rootId;
    }

    private static string? HitRelation(
        AutomationElement target,
        AutomationElement hit,
        UIA3Automation automation)
    {
        if (SafeCompare(target, hit, automation))
        {
            return "self";
        }

        var current = hit;
        for (var depth = 0; depth < MaxAncestorDepth; depth++)
        {
            AutomationElement parent;
            try
            {
                parent = current.Parent;
            }
            catch
            {
                return null;
            }

            if (parent is null)
            {
                return null;
            }
            if (SafeCompare(target, parent, automation))
            {
                return "descendant";
            }
            current = parent;
        }
        return null;
    }

    private static bool SafeCompare(
        AutomationElement left,
        AutomationElement right,
        UIA3Automation automation)
    {
        try
        {
            return automation.Compare(left, right);
        }
        catch
        {
            return false;
        }
    }

    private static IntPtr SafeWindowHandle(AutomationElement element)
    {
        try
        {
            return element.Properties.NativeWindowHandle.ValueOrDefault;
        }
        catch
        {
            return IntPtr.Zero;
        }
    }

    private static int SafeProcessId(AutomationElement element)
    {
        try
        {
            return element.Properties.ProcessId.ValueOrDefault;
        }
        catch
        {
            return 0;
        }
    }

    private static Rectangle VirtualScreenBounds()
    {
        return new Rectangle(
            GetSystemMetrics(SmXVirtualScreen),
            GetSystemMetrics(SmYVirtualScreen),
            GetSystemMetrics(SmCxVirtualScreen),
            GetSystemMetrics(SmCyVirtualScreen));
    }

    private static JsonObject RequestedSelector(JsonNode? @params)
    {
        return new JsonObject
        {
            [AutomationIdKey] = ParamString(@params, AutomationIdKey),
            ["name"] = ParamString(@params, "name"),
            [ControlTypeKey] = ParamString(@params, ControlTypeKey),
            [RootAutomationIdKey] = ParamString(@params, RootAutomationIdKey),
            [XPathKey] = ParamString(@params, XPathKey),
        };
    }

    private static JsonObject RectJson(Rectangle rect)
    {
        return new JsonObject
        {
            ["x"] = rect.X,
            ["y"] = rect.Y,
            ["width"] = rect.Width,
            ["height"] = rect.Height,
        };
    }

    private static JsonObject PointJson(Point point)
    {
        return new JsonObject
        {
            ["x"] = point.X,
            ["y"] = point.Y,
        };
    }

    private static int ReadTimeoutMs(JsonNode? @params)
    {
        var node = @params?["timeoutMs"];
        if (node is null)
        {
            return 5000;
        }
        if (node is JsonValue value && value.TryGetValue<int>(out var timeoutMs) &&
            timeoutMs is >= 1 and <= 30000)
        {
            return timeoutMs;
        }
        throw new ArgumentException("timeoutMs must be an integer from 1 to 30000");
    }

    private static string? ParamString(JsonNode? @params, string key)
    {
        try
        {
            return @params?[key]?.GetValue<string>();
        }
        catch
        {
            return null;
        }
    }

    private static ControlType ParseControlType(string controlType)
    {
        if (!Enum.TryParse<ControlType>(controlType, ignoreCase: true, out var parsed) ||
            !Enum.IsDefined(typeof(ControlType), parsed))
        {
            throw new ArgumentException($"Unknown controlType: {controlType}");
        }
        return parsed;
    }
}
