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

public static class HoverCommands
{
    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetCursorPos(out NativePoint point);

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int index);

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
                "BLOCKED",
                "selector-scoped pointer hover is unavailable in stealth mode",
                "stealth",
                timeoutMs,
                stopwatch,
                requested: RequestedSelector(@params),
                accepted: new JsonObject
                {
                    ["stealth"] = false,
                    ["capability"] = "foreground physical pointer hover",
                },
                nextStep: "Reconnect without stealth mode and establish the exact target foreground window.");
        }

        var deadlineFailure = CheckDeadline(stopwatch, timeoutMs, "resolve_root", pointerMoved);
        if (deadlineFailure is not null)
        {
            return deadlineFailure;
        }

        var (searchRoot, targetWindow, rootFailure) = ResolveHoverRoot(
            mainWindow,
            @params,
            automation,
            timeoutMs,
            stopwatch);
        if (rootFailure is not null)
        {
            return rootFailure;
        }

        deadlineFailure = CheckDeadline(stopwatch, timeoutMs, "resolve_target", pointerMoved);
        if (deadlineFailure is not null)
        {
            return deadlineFailure;
        }

        var (target, resolvedSelector, targetFailure) = ResolveUniqueTarget(
            searchRoot!,
            @params,
            automation,
            timeoutMs,
            stopwatch);
        if (targetFailure is not null)
        {
            return targetFailure;
        }

        deadlineFailure = CheckDeadline(stopwatch, timeoutMs, "validate_target", pointerMoved);
        if (deadlineFailure is not null)
        {
            return deadlineFailure;
        }

        var targetRootHwnd = SafeWindowHandle(targetWindow!);
        var targetProcessId = SafeProcessId(targetWindow!);
        if (targetRootHwnd == IntPtr.Zero || targetProcessId <= 0 ||
            targetProcessId != JsonRpcHandler.ProcessId ||
            SafeProcessId(target!) != JsonRpcHandler.ProcessId)
        {
            return NotStartedFailure(
                "BLOCKED",
                "hover target does not belong to a usable top-level window in the attached process",
                "validate_target",
                timeoutMs,
                stopwatch,
                requested: RequestedSelector(@params),
                accepted: new JsonObject
                {
                    ["targetProcessId"] = JsonRpcHandler.ProcessId,
                    ["targetRootHwnd"] = "non-zero HWND owned by the attached process",
                },
                nextStep: "Reconnect to the target process and resolve a target inside one of its top-level windows.",
                extra: new JsonObject
                {
                    ["targetRootHwnd"] = targetRootHwnd.ToInt64(),
                    ["targetProcessId"] = targetProcessId,
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
                "BLOCKED",
                $"hover target bounds are unavailable: {ex.Message}",
                "validate_target",
                timeoutMs,
                stopwatch,
                requested: RequestedSelector(@params),
                accepted: new JsonObject { ["targetRect"] = "positive on-screen rectangle" },
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
                "BLOCKED",
                "hover target is off-screen or its actionable point is outside virtual-screen bounds",
                "validate_target",
                timeoutMs,
                stopwatch,
                requested: RequestedSelector(@params),
                accepted: new JsonObject { ["targetRect"] = "positive rectangle with an actionable point inside the virtual screen" },
                nextStep: "Scroll or move the target so its center point is on-screen before hovering.",
                extra: new JsonObject
                {
                    ["targetRect"] = RectJson(targetRect),
                    ["virtualScreen"] = RectJson(virtualScreen),
                    ["isOffscreen"] = isOffscreen,
                });
        }
        var foregroundHwndBefore = GetForegroundWindow();
        if (foregroundHwndBefore != targetRootHwnd)
        {
            return NotStartedFailure(
                "BLOCKED",
                "exact target-root foreground prerequisite is not satisfied",
                "foreground_before",
                timeoutMs,
                stopwatch,
                requested: RequestedSelector(@params),
                accepted: new JsonObject { ["foregroundHwnd"] = targetRootHwnd.ToInt64() },
                nextStep: "Use ui.input.ensure_target or harness setup to foreground the exact target root, then retry.",
                extra: new JsonObject
                {
                    ["targetRootHwnd"] = targetRootHwnd.ToInt64(),
                    ["targetProcessId"] = targetProcessId,
                    ["foregroundHwndBefore"] = foregroundHwndBefore.ToInt64(),
                    ["foregroundVerified"] = false,
                    ["targetRect"] = RectJson(targetRect),
                    ["requestedPoint"] = PointJson(requestedPoint),
                });
        }

        AutomationElement focusBefore;
        try
        {
            focusBefore = automation.FocusedElement();
            if (focusBefore is null)
            {
                return NotStartedFailure(
                    "BLOCKED",
                    "focused-element evidence is unavailable before hover",
                    "focus_before",
                    timeoutMs,
                    stopwatch,
                    requested: RequestedSelector(@params),
                    accepted: new JsonObject { ["focusBefore"] = "non-null focused AutomationElement" },
                    nextStep: "Establish keyboard focus inside the target window and retry.");
            }
        }
        catch (Exception ex)
        {
            return NotStartedFailure(
                "BLOCKED",
                $"focused-element evidence is unavailable before hover: {ex.Message}",
                "focus_before",
                timeoutMs,
                stopwatch,
                requested: RequestedSelector(@params),
                accepted: new JsonObject { ["focusBefore"] = "readable focused AutomationElement" },
                nextStep: "Establish keyboard focus inside the target window and retry.");
        }

        deadlineFailure = CheckDeadline(
            stopwatch,
            timeoutMs,
            "move_pointer",
            pointerMoved,
            PointerSettleMs);
        if (deadlineFailure is not null)
        {
            return deadlineFailure;
        }

        ClickCommands.MoveCursor(requestedPoint.X, requestedPoint.Y);
        pointerMoved = true;
        Thread.Sleep(PointerSettleMs);

        deadlineFailure = CheckDeadline(stopwatch, timeoutMs, "pointer_readback", pointerMoved);
        if (deadlineFailure is not null)
        {
            return deadlineFailure;
        }

        if (!GetCursorPos(out var nativePoint))
        {
            return MovedFailure(
                "FAIL",
                $"GetCursorPos failed with Win32 error {Marshal.GetLastWin32Error()}",
                "pointer_readback",
                timeoutMs,
                stopwatch,
                targetRootHwnd,
                targetProcessId,
                foregroundHwndBefore,
                targetRect,
                requestedPoint);
        }

        var actualPointer = new Point(nativePoint.X, nativePoint.Y);
        if (!targetRect.Contains(actualPointer))
        {
            return MovedFailure(
                "FAIL",
                "actual pointer is outside the resolved hover target",
                "pointer_readback",
                timeoutMs,
                stopwatch,
                targetRootHwnd,
                targetProcessId,
                foregroundHwndBefore,
                targetRect,
                requestedPoint,
                actualPointer);
        }

        AutomationElement? hitElement;
        string? hitRelation;
        try
        {
            hitElement = automation.FromPoint(actualPointer);
            if (hitElement is null)
            {
                return MovedFailure(
                    "FAIL",
                    "UIA hit-test returned no element after pointer movement",
                    "hit_test",
                    timeoutMs,
                    stopwatch,
                    targetRootHwnd,
                    targetProcessId,
                    foregroundHwndBefore,
                    targetRect,
                    requestedPoint,
                    actualPointer);
            }
            hitRelation = HitRelation(target!, hitElement, automation);
        }
        catch (Exception ex)
        {
            return MovedFailure(
                "FAIL",
                $"UIA hit-test failed after pointer movement: {ex.Message}",
                "hit_test",
                timeoutMs,
                stopwatch,
                targetRootHwnd,
                targetProcessId,
                foregroundHwndBefore,
                targetRect,
                requestedPoint,
                actualPointer);
        }

        if (hitRelation is null)
        {
            return MovedFailure(
                "FAIL",
                "UIA hit-test does not resolve to the hover target or one of its descendants",
                "hit_test",
                timeoutMs,
                stopwatch,
                targetRootHwnd,
                targetProcessId,
                foregroundHwndBefore,
                targetRect,
                requestedPoint,
                actualPointer,
                new JsonObject
                {
                    ["hitElement"] = ElementCommands.BuildElementInfo(hitElement, includePatterns: false),
                    ["hitRelation"] = "unrelated",
                    ["underPointer"] = false,
                });
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
                    "FAIL",
                    "focused-element evidence is unavailable after hover",
                    "focus_after",
                    timeoutMs,
                    stopwatch,
                    targetRootHwnd,
                    targetProcessId,
                    foregroundHwndBefore,
                    targetRect,
                    requestedPoint,
                    actualPointer);
            }
        }
        catch (Exception ex)
        {
            return MovedFailure(
                "FAIL",
                $"focused-element evidence is unavailable after hover: {ex.Message}",
                "focus_after",
                timeoutMs,
                stopwatch,
                targetRootHwnd,
                targetProcessId,
                foregroundHwndBefore,
                targetRect,
                requestedPoint,
                actualPointer);
        }

        var focusUnchanged = SafeCompare(focusBefore, focusAfter, automation);
        var foregroundHwndAfter = GetForegroundWindow();
        var foregroundVerified = foregroundHwndBefore == targetRootHwnd &&
            foregroundHwndAfter == targetRootHwnd;

        if (!foregroundVerified || !focusUnchanged)
        {
            return MovedFailure(
                "FAIL",
                !foregroundVerified
                    ? "target-root foreground changed during hover"
                    : "keyboard focus changed during hover",
                !foregroundVerified ? "foreground_after" : "focus_after",
                timeoutMs,
                stopwatch,
                targetRootHwnd,
                targetProcessId,
                foregroundHwndBefore,
                targetRect,
                requestedPoint,
                actualPointer,
                new JsonObject
                {
                    ["foregroundHwndAfter"] = foregroundHwndAfter.ToInt64(),
                    ["foregroundVerified"] = foregroundVerified,
                    ["focusBefore"] = ElementCommands.BuildElementInfo(focusBefore, includePatterns: false),
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
            ["target"] = ElementCommands.BuildElementInfo(target!, includePatterns: false),
            ["matchCount"] = 1,
            ["targetRootHwnd"] = targetRootHwnd.ToInt64(),
            ["targetProcessId"] = targetProcessId,
            ["foregroundHwndBefore"] = foregroundHwndBefore.ToInt64(),
            ["foregroundHwndAfter"] = foregroundHwndAfter.ToInt64(),
            ["foregroundVerified"] = true,
            ["focusBefore"] = ElementCommands.BuildElementInfo(focusBefore, includePatterns: false),
            ["focusAfter"] = ElementCommands.BuildElementInfo(focusAfter, includePatterns: false),
            ["focusUnchanged"] = true,
            ["targetRect"] = RectJson(targetRect),
            ["requestedPoint"] = PointJson(requestedPoint),
            ["actualPointer"] = PointJson(actualPointer),
            ["hitElement"] = ElementCommands.BuildElementInfo(hitElement, includePatterns: false),
            ["hitRelation"] = hitRelation,
            ["underPointer"] = true,
            ["hovered"] = true,
            ["click"] = false,
            ["button"] = "none",
            ["timeoutMs"] = timeoutMs,
            ["elapsedMs"] = stopwatch.ElapsedMilliseconds,
            ["pointerMutationState"] = "moved",
        };
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
        var rootId = ParamString(@params, "rootAutomationId");
        if (string.IsNullOrWhiteSpace(rootId))
        {
            return (mainWindow, mainWindow, null);
        }

        var rootMatches = new List<(AutomationElement Element, AutomationElement Window)>();
        var condition = new ConditionFactory(automation.PropertyLibrary).ByAutomationId(rootId);
        foreach (var window in ElementCommands.GetProcessTopLevelWindows(mainWindow, automation))
        {
            if (MatchesRootIdentity(window, rootId))
            {
                AddUniqueRoot(rootMatches, window, window, automation);
            }

            AutomationElement[] descendants;
            try
            {
                descendants = window.FindAllDescendants(condition);
            }
            catch (Exception ex)
            {
                return (
                    null,
                    null,
                    NotStartedFailure(
                        "BLOCKED",
                        $"hover root enumeration failed: {ex.Message}",
                        "resolve_root",
                        timeoutMs,
                        stopwatch,
                        requested: new JsonObject { ["rootAutomationId"] = rootId },
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
                    "BLOCKED",
                    rootMatches.Count == 0
                        ? "hover root selector did not match any element"
                        : "hover root selector is ambiguous",
                    "resolve_root",
                    timeoutMs,
                    stopwatch,
                    requested: new JsonObject { ["rootAutomationId"] = rootId },
                    accepted: new JsonObject { ["matchCount"] = 1 },
                    nextStep: "Use a root_id that resolves to exactly one element across the target process.",
                    extra: new JsonObject { ["matchCount"] = rootMatches.Count }));
        }

        return (rootMatches[0].Element, rootMatches[0].Window, null);
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
        var automationId = ParamString(@params, "automationId");
        var xpath = ParamString(@params, "xpath");
        var name = ParamString(@params, "name");
        var controlType = ParamString(@params, "controlType");
        JsonObject? lastSelector = null;
        var targetMatches = new List<AutomationElement>();

        if (!string.IsNullOrWhiteSpace(automationId))
        {
            lastSelector = new JsonObject
            {
                ["criterion"] = "automationId",
                ["automationId"] = automationId,
            };
            targetMatches = searchRoot.FindAllDescendants(cf.ByAutomationId(automationId)).ToList();
            if (targetMatches.Count > 0)
            {
                return UniqueTargetResult(targetMatches, lastSelector, @params, timeoutMs, stopwatch);
            }
        }

        if (!string.IsNullOrWhiteSpace(xpath))
        {
            lastSelector = new JsonObject
            {
                ["criterion"] = "xpath",
                ["xpath"] = xpath,
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
                        "BLOCKED",
                        $"hover XPath selector is invalid or unavailable: {ex.Message}",
                        "resolve_target",
                        timeoutMs,
                        stopwatch,
                        requested: RequestedSelector(@params),
                        accepted: new JsonObject { ["xpath"] = "valid FlaUI XPath" },
                        nextStep: "Correct the XPath or use automation_id/name/control_type."));
            }
            if (targetMatches.Count > 0)
            {
                return UniqueTargetResult(targetMatches, lastSelector, @params, timeoutMs, stopwatch);
            }
        }

        if (!string.IsNullOrWhiteSpace(name) || !string.IsNullOrWhiteSpace(controlType))
        {
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
                    return (
                        null,
                        null,
                        NotStartedFailure(
                            "BLOCKED",
                            ex.Message,
                            "resolve_target",
                            timeoutMs,
                            stopwatch,
                            requested: RequestedSelector(@params),
                            accepted: new JsonObject { ["controlType"] = "valid FlaUI ControlType" },
                            nextStep: "Provide a valid controlType."));
                }
            }
            var condition = conditions.Count == 1
                ? conditions[0]
                : new AndCondition(conditions.ToArray());
            lastSelector = new JsonObject
            {
                ["criterion"] = "name+controlType",
                ["name"] = name,
                ["controlType"] = controlType,
            };
            targetMatches = searchRoot.FindAllDescendants(condition).ToList();
            if (targetMatches.Count > 0)
            {
                return UniqueTargetResult(targetMatches, lastSelector, @params, timeoutMs, stopwatch);
            }
        }

        return (
            null,
            null,
            NotStartedFailure(
                "BLOCKED",
                lastSelector is null
                    ? "hover requires a target selector"
                    : "hover target selector did not match any element",
                "resolve_target",
                timeoutMs,
                stopwatch,
                requested: RequestedSelector(@params),
                accepted: new JsonObject
                {
                    ["selector"] = "automationId, xpath, or name/controlType with exactly one match",
                    ["matchCount"] = 1,
                },
                nextStep: "Inspect the scoped tree and provide a selector that resolves to exactly one target.",
                extra: new JsonObject
                {
                    ["resolvedSelector"] = lastSelector,
                    ["matchCount"] = 0,
                }));
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
                "BLOCKED",
                "hover target selector is ambiguous",
                "resolve_target",
                timeoutMs,
                stopwatch,
                requested: RequestedSelector(@params),
                accepted: new JsonObject { ["matchCount"] = 1 },
                nextStep: "Add a unique root_id or stronger selector so exactly one target matches.",
                extra: new JsonObject
                {
                    ["resolvedSelector"] = resolvedSelector,
                    ["matchCount"] = targetMatches.Count,
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
            ["status"] = "BLOCKED",
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
        string status,
        string reason,
        string phase,
        int timeoutMs,
        Stopwatch stopwatch,
        JsonObject requested,
        JsonObject accepted,
        string nextStep,
        JsonObject? extra = null)
    {
        var result = Failure(status, reason, phase, timeoutMs, stopwatch, requested, accepted, nextStep);
        result["pointerMutationState"] = "not_started";
        Merge(result, extra);
        return result;
    }

    private static JsonObject MovedFailure(
        string status,
        string reason,
        string phase,
        int timeoutMs,
        Stopwatch stopwatch,
        IntPtr targetRootHwnd,
        int targetProcessId,
        IntPtr foregroundHwndBefore,
        Rectangle targetRect,
        Point requestedPoint,
        Point? actualPointer = null,
        JsonObject? extra = null)
    {
        var result = Failure(
            status,
            reason,
            phase,
            timeoutMs,
            stopwatch,
            new JsonObject { ["targetRootHwnd"] = targetRootHwnd.ToInt64() },
            new JsonObject { ["hoverEvidence"] = "complete and internally consistent" },
            "Re-establish target foreground/focus/visibility, inspect occlusion, and retry.");
        result["pointerMutationState"] = "moved";
        result["targetRootHwnd"] = targetRootHwnd.ToInt64();
        result["targetProcessId"] = targetProcessId;
        result["foregroundHwndBefore"] = foregroundHwndBefore.ToInt64();
        result["targetRect"] = RectJson(targetRect);
        result["requestedPoint"] = PointJson(requestedPoint);
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
        int timeoutMs,
        Stopwatch stopwatch,
        JsonObject requested,
        JsonObject accepted,
        string nextStep)
    {
        return new JsonObject
        {
            ["status"] = status,
            ["reason"] = reason,
            ["phase"] = phase,
            ["timeoutMs"] = timeoutMs,
            ["elapsedMs"] = stopwatch.ElapsedMilliseconds,
            ["requested"] = requested,
            ["accepted"] = accepted,
            ["next_step"] = nextStep,
        };
    }

    private static void MarkMutationState(JsonObject result, bool pointerMoved)
    {
        if (pointerMoved)
        {
            result["pointerMutationState"] = "moved";
            return;
        }
        result["pointerMutationState"] = "not_started";
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
        try
        {
            return element.Properties.AutomationId.IsSupported && element.AutomationId == rootId;
        }
        catch
        {
            return false;
        }
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
            ["automationId"] = ParamString(@params, "automationId"),
            ["name"] = ParamString(@params, "name"),
            ["controlType"] = ParamString(@params, "controlType"),
            ["rootAutomationId"] = ParamString(@params, "rootAutomationId"),
            ["xpath"] = ParamString(@params, "xpath"),
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
        try
        {
            var timeoutMs = node.GetValue<int>();
            if (timeoutMs is >= 1 and <= 30000)
            {
                return timeoutMs;
            }
        }
        catch
        {
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
