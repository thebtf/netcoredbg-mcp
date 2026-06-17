using System.Drawing;
using System.Diagnostics;
using System.Runtime.ExceptionServices;
using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Input;
using FlaUI.Core.Tools;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class ClickCommands
{
    private sealed record DragPathPoint(int X, int Y, int HoldMs);

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr processId);

    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern bool BringWindowToTop(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetCursorPos(int x, int y);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern void mouse_event(
        uint dwFlags,
        uint dx,
        uint dy,
        uint dwData,
        UIntPtr dwExtraInfo);

    private const int SW_RESTORE = 9;
    private const int DragThresholdPixels = 5;
    private const int ForegroundActivationTimeoutMs = 750;
    private const int ForegroundActivationPollMs = 25;
    private const int PointerMoveSettleMs = 50;
    private const int PointerDownSettleMs = 100;
    private const int DragPathHoldPulseMs = 100;
    private const int FinalDropSettleMs = 180;
    private const uint MOUSEEVENTF_MOVE = 0x0001;
    private const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    private const uint MOUSEEVENTF_LEFTUP = 0x0004;

    public static JsonNode Click(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var automationId = @params?["automationId"]?.GetValue<string>();

        if (automationId is not null)
        {
            return ClickByAutomationId(automationId, automation, mainWindow);
        }

        var x = @params?["x"]?.GetValue<int>();
        var y = @params?["y"]?.GetValue<int>();

        if (x is not null && y is not null)
        {
            if (JsonRpcHandler.Stealth)
            {
                return FlashFocusClick(x.Value, y.Value, mainWindow);
            }

            EnsureForeground(mainWindow);
            Mouse.Click(new Point(x.Value, y.Value));
            return new JsonObject { ["clicked"] = true, ["x"] = x.Value, ["y"] = y.Value };
        }

        throw new ArgumentException("Provide 'automationId' or 'x'/'y' coordinates");
    }

    public static JsonNode RightClick(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        RejectStealthMouseInput("right_click");
        var (x, y) = GetCoordinates(@params);
        EnsureForeground(mainWindow);
        Mouse.RightClick(new Point(x, y));
        return new JsonObject { ["rightClicked"] = true, ["x"] = x, ["y"] = y };
    }

    public static JsonNode DoubleClick(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        RejectStealthMouseInput("double_click");
        var (x, y) = GetCoordinates(@params);
        EnsureForeground(mainWindow);
        Mouse.DoubleClick(new Point(x, y));
        return new JsonObject { ["doubleClicked"] = true, ["x"] = x, ["y"] = y };
    }

    public static JsonNode Drag(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        RejectStealthMouseInput("drag");
        var x1 = @params?["x1"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing 'x1'");
        var y1 = @params?["y1"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing 'y1'");
        var x2 = @params?["x2"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing 'x2'");
        var y2 = @params?["y2"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing 'y2'");
        var speedMs = @params?["speed_ms"]?.GetValue<int>() ?? 200;

        if (speedMs < 20)
            throw new ArgumentException("speed_ms below drag-threshold safety floor (minimum 20)");

        if (x1 == x2 && y1 == y2)
            throw new ArgumentException("from and to coordinates are identical (0 px distance)");

        if (Math.Abs(x2 - x1) < DragThresholdPixels && Math.Abs(y2 - y1) < DragThresholdPixels)
        {
            throw new ArgumentException(
                $"drag distance below WPF threshold (<{DragThresholdPixels} px in each axis); adjust coordinates or use ui_click");
        }

        var steps = Math.Max(10, speedMs / 20);
        var temporaryModifiers = ModifierCommands.GetTemporaryModifierKeys(@params?["hold_modifiers"]);
        var pressedTemporaryModifiers = new List<FlaUI.Core.WindowsAPI.VirtualKeyShort>();
        var mouseButtonDown = false;
        ExceptionDispatchInfo? capturedException = null;
        Exception? cleanupException = null;

        EnsureForeground(mainWindow);

        var stopwatch = Stopwatch.StartNew();
        IReadOnlyList<Point> waypoints = Array.Empty<Point>();

        try
        {
            foreach (var modifier in temporaryModifiers)
            {
                Keyboard.Press(modifier);
                pressedTemporaryModifiers.Add(modifier);
            }

            MoveCursor(x1, y1);
            Thread.Sleep(PointerMoveSettleMs);
            mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, UIntPtr.Zero);
            mouseButtonDown = true;
            Thread.Sleep(PointerDownSettleMs);

            waypoints = BuildDragWaypoints(x1, y1, x2, y2, steps);
            var delayMs = Math.Max(1, (int)Math.Round(speedMs / (double)steps));

            foreach (var waypoint in waypoints)
            {
                MoveCursor(waypoint.X, waypoint.Y);
                Thread.Sleep(delayMs);
            }

            Thread.Sleep(Math.Max(FinalDropSettleMs, delayMs));
        }
        catch (Exception ex)
        {
            capturedException = ExceptionDispatchInfo.Capture(ex);
        }
        finally
        {
            stopwatch.Stop();
            if (mouseButtonDown)
            {
                try
                {
                    mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, UIntPtr.Zero);
                }
                catch (Exception ex)
                {
                    Program.Log($"Drag cleanup failed to release left mouse button: {ex.Message}");
                    cleanupException ??= new InvalidOperationException(
                        "Drag cleanup failed to release left mouse button.",
                        ex);
                }
            }

            for (var i = pressedTemporaryModifiers.Count - 1; i >= 0; i--)
            {
                try
                {
                    Keyboard.Release(pressedTemporaryModifiers[i]);
                }
                catch (Exception ex)
                {
                    Program.Log($"Drag cleanup failed to release modifier {pressedTemporaryModifiers[i]}: {ex.Message}");
                    cleanupException ??= new InvalidOperationException(
                        $"Drag cleanup failed to release modifier {pressedTemporaryModifiers[i]}.",
                        ex);
                }
            }
        }

        capturedException?.Throw();
        if (cleanupException is not null)
            throw cleanupException;

        return new JsonObject
        {
            ["dragged"] = true,
            ["x1"] = x1,
            ["y1"] = y1,
            ["x2"] = x2,
            ["y2"] = y2,
            ["path_points"] = DragPointsJson(new[] { new Point(x1, y1) }.Concat(waypoints)),
            ["final_pointer"] = DragPointJson(new Point(x2, y2)),
            ["steps"] = steps,
            ["duration_ms"] = stopwatch.ElapsedMilliseconds
        };
    }

    public static JsonNode DragPath(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        RejectStealthMouseInput("drag_path");

        var speedMs = 200;
        if (@params is JsonObject paramObject &&
            TryReadInt(paramObject, "speed_ms", out var requestedSpeedMs))
        {
            speedMs = requestedSpeedMs;
        }
        if (speedMs < 20)
        {
            return DragPathBlocked(
                "speed_ms below drag-path safety floor",
                new JsonObject { ["speed_ms"] = speedMs },
                new JsonObject { ["speed_ms"] = "integer >= 20" },
                "Increase speed_ms to at least 20 for path-aware drag.");
        }

        var (points, blocked) = ParseDragPathPoints(@params?["points"]);
        if (blocked is not null)
        {
            return blocked;
        }

        var (cancelKey, cancelBlocked) = TryReadDragPathCancelKey(@params?["cancel_key"]);
        if (cancelBlocked is not null)
        {
            return cancelBlocked;
        }

        var temporaryModifiers = ModifierCommands.GetTemporaryModifierKeys(@params?["hold_modifiers"]);
        var pressedTemporaryModifiers = new List<FlaUI.Core.WindowsAPI.VirtualKeyShort>();
        var mouseButtonDown = false;
        var cancelSent = false;
        ExceptionDispatchInfo? capturedException = null;
        Exception? cleanupException = null;

        EnsureForeground(mainWindow);

        var stopwatch = Stopwatch.StartNew();
        var delayMs = Math.Max(1, (int)Math.Round(speedMs / (double)Math.Max(1, points.Count - 1)));

        try
        {
            foreach (var modifier in temporaryModifiers)
            {
                Keyboard.Press(modifier);
                pressedTemporaryModifiers.Add(modifier);
            }

            MoveCursor(points[0].X, points[0].Y);
            Thread.Sleep(PointerMoveSettleMs);
            mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, UIntPtr.Zero);
            mouseButtonDown = true;
            Thread.Sleep(PointerDownSettleMs);

            for (var index = 1; index < points.Count; index++)
            {
                var point = points[index];
                MoveCursor(point.X, point.Y);
                Thread.Sleep(delayMs);
                if (point.HoldMs > 0)
                {
                    PulseHeldDragPoint(point, point.HoldMs);
                }
            }

            if (cancelKey is not null)
            {
                SendDragPathCancel(cancelKey.Value);
                cancelSent = true;
            }

            Thread.Sleep(Math.Max(FinalDropSettleMs, delayMs));
        }
        catch (Exception ex)
        {
            capturedException = ExceptionDispatchInfo.Capture(ex);
        }
        finally
        {
            stopwatch.Stop();
            if (mouseButtonDown)
            {
                try
                {
                    mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, UIntPtr.Zero);
                }
                catch (Exception ex)
                {
                    Program.Log($"Drag path cleanup failed to release left mouse button: {ex.Message}");
                    cleanupException ??= new InvalidOperationException(
                        "Drag path cleanup failed to release left mouse button.",
                        ex);
                }
            }

            for (var i = pressedTemporaryModifiers.Count - 1; i >= 0; i--)
            {
                try
                {
                    Keyboard.Release(pressedTemporaryModifiers[i]);
                }
                catch (Exception ex)
                {
                    Program.Log($"Drag path cleanup failed to release modifier {pressedTemporaryModifiers[i]}: {ex.Message}");
                    cleanupException ??= new InvalidOperationException(
                        $"Drag path cleanup failed to release modifier {pressedTemporaryModifiers[i]}.",
                        ex);
                }
            }
        }

        capturedException?.Throw();
        if (cleanupException is not null)
            throw cleanupException;

        var releasedModifiers = new JsonArray();
        foreach (var modifier in pressedTemporaryModifiers)
        {
            releasedModifiers.Add(modifier.ToString());
        }

        var output = new JsonObject
        {
            ["dragged"] = true,
            ["path_points"] = DragPathPointsJson(points),
            ["hold_points"] = DragPathPointsJson(points.Where(point => point.HoldMs > 0)),
            ["final_pointer"] = DragPathPointJson(points[^1]),
            ["modifier_cleanup"] = new JsonObject { ["released"] = releasedModifiers },
            ["pointer_cleanup"] = new JsonObject { ["left_button_released"] = true },
            ["steps"] = Math.Max(0, points.Count - 1),
            ["duration_ms"] = stopwatch.ElapsedMilliseconds
        };
        if (cancelSent)
        {
            output["cancel"] = new JsonObject { ["key"] = "escape", ["sent"] = true };
            output["no_op"] = new JsonObject
            {
                ["expected"] = true,
                ["reason"] = "cancelled",
                ["route_attempted"] = true
            };
        }

        return output;
    }

    private static void MoveCursor(int x, int y)
    {
        if (!SetCursorPos(x, y))
        {
            var error = System.Runtime.InteropServices.Marshal.GetLastWin32Error();
            throw new InvalidOperationException($"SetCursorPos failed with Win32 error {error}");
        }

        mouse_event(MOUSEEVENTF_MOVE, 0, 0, 0, UIntPtr.Zero);
    }

    private static void PulseHeldDragPoint(DragPathPoint point, int holdMs)
    {
        var elapsedMs = 0;
        var offset = 1;
        while (elapsedMs < holdMs)
        {
            var sleepMs = Math.Min(DragPathHoldPulseMs, holdMs - elapsedMs);
            Thread.Sleep(sleepMs);
            elapsedMs += sleepMs;
            if (elapsedMs >= holdMs)
            {
                break;
            }

            MoveCursor(point.X + offset, point.Y);
            offset = -offset;
        }

        MoveCursor(point.X, point.Y);
    }

    private static void SendDragPathCancel(FlaUI.Core.WindowsAPI.VirtualKeyShort cancelKey)
    {
        Keyboard.Press(cancelKey);
        Keyboard.Release(cancelKey);
    }

    private static (List<DragPathPoint> Points, JsonObject? Blocked) ParseDragPathPoints(
        JsonNode? pointsNode)
    {
        if (pointsNode is not JsonArray pointsArray || pointsArray.Count < 2)
        {
            return (
                new List<DragPathPoint>(),
                DragPathBlocked(
                    "drag_path requires at least two points",
                    new JsonObject { ["points_count"] = pointsNode is JsonArray array ? array.Count : 0 },
                    new JsonObject { ["points"] = "array with at least two screen points" },
                    "Provide start and end points for path-aware drag."));
        }

        var points = new List<DragPathPoint>(pointsArray.Count);
        for (var index = 0; index < pointsArray.Count; index++)
        {
            if (pointsArray[index] is not JsonObject pointObject)
            {
                return (
                    new List<DragPathPoint>(),
                    DragPathBlocked(
                        "drag_path point must be an object",
                        new JsonObject { ["point_index"] = index },
                        new JsonObject { ["point"] = "object with integer x and y" },
                        "Provide every path point as an object."));
            }

            if (!TryReadInt(pointObject, "x", out var x) || !TryReadInt(pointObject, "y", out var y))
            {
                return (
                    new List<DragPathPoint>(),
                    DragPathBlocked(
                        "drag_path point requires integer x and y",
                        new JsonObject { ["point_index"] = index },
                        new JsonObject { ["x"] = "integer", ["y"] = "integer" },
                        "Provide integer screen coordinates for each path point."));
            }

            var holdMs = 0;
            if (pointObject.TryGetPropertyValue("hold_ms", out var holdNode))
            {
                if (holdNode is null || !TryReadInt(pointObject, "hold_ms", out holdMs))
                {
                    return (
                        new List<DragPathPoint>(),
                        DragPathBlocked(
                            "hold_ms must be an integer",
                            new JsonObject { ["point_index"] = index },
                            new JsonObject { ["hold_ms"] = "integer >= 0" },
                            "Use a non-negative integer hold_ms value."));
                }

                if (holdMs < 0)
                {
                    return (
                        new List<DragPathPoint>(),
                        DragPathBlocked(
                            "hold_ms must be non-negative",
                            new JsonObject { ["point_index"] = index, ["hold_ms"] = holdMs },
                            new JsonObject { ["hold_ms"] = "integer >= 0" },
                            "Use a non-negative hold duration for path-aware drag."));
                }
            }

            points.Add(new DragPathPoint(x, y, holdMs));
        }

        var firstPoint = points[0];
        if (!points.Skip(1).Any(point => point.X != firstPoint.X || point.Y != firstPoint.Y))
        {
            return (
                new List<DragPathPoint>(),
                DragPathBlocked(
                    "drag_path route requires pointer movement",
                    new JsonObject { ["points_count"] = points.Count },
                    new JsonObject { ["points"] = "at least one point after start must move" },
                    "Provide a path that moves away from the start point."));
        }

        return (points, null);
    }

    private static (FlaUI.Core.WindowsAPI.VirtualKeyShort? CancelKey, JsonObject? Blocked) TryReadDragPathCancelKey(
        JsonNode? cancelNode)
    {
        if (cancelNode is null)
        {
            return (null, null);
        }

        string cancelText;
        try
        {
            cancelText = cancelNode.GetValue<string>();
        }
        catch (Exception)
        {
            return (
                null,
                DragPathBlocked(
                    "cancel_key must be a string",
                    new JsonObject { ["cancel_key"] = cancelNode.ToJsonString() },
                    new JsonObject { ["cancel_key"] = "escape" },
                    "Use cancel_key: escape for path-aware drag cancellation."));
        }

        if (string.Equals(cancelText, "escape", StringComparison.OrdinalIgnoreCase) ||
            string.Equals(cancelText, "esc", StringComparison.OrdinalIgnoreCase))
        {
            return (FlaUI.Core.WindowsAPI.VirtualKeyShort.ESCAPE, null);
        }

        return (
            null,
            DragPathBlocked(
                "unsupported drag_path cancel_key",
                new JsonObject { ["cancel_key"] = cancelText },
                new JsonObject { ["cancel_key"] = "escape" },
                "Use cancel_key: escape for path-aware drag cancellation."));
    }

    private static bool TryReadInt(JsonObject value, string key, out int result)
    {
        result = 0;
        try
        {
            if (value[key] is null)
            {
                return false;
            }

            result = value[key]!.GetValue<int>();
            return true;
        }
        catch
        {
            return false;
        }
    }

    private static JsonArray DragPathPointsJson(IEnumerable<DragPathPoint> points)
    {
        var result = new JsonArray();
        foreach (var point in points)
        {
            result.Add(DragPathPointJson(point));
        }

        return result;
    }

    private static JsonObject DragPathPointJson(DragPathPoint point)
    {
        var result = new JsonObject
        {
            ["x"] = point.X,
            ["y"] = point.Y
        };
        if (point.HoldMs > 0)
        {
            result["hold_ms"] = point.HoldMs;
        }

        return result;
    }

    private static JsonArray DragPointsJson(IEnumerable<Point> points)
    {
        var result = new JsonArray();
        foreach (var point in points)
        {
            result.Add(DragPointJson(point));
        }

        return result;
    }

    private static JsonObject DragPointJson(Point point)
    {
        return new JsonObject
        {
            ["x"] = point.X,
            ["y"] = point.Y
        };
    }

    private static JsonObject DragPathBlocked(
        string reason,
        JsonObject requested,
        JsonObject accepted,
        string nextStep)
    {
        return new JsonObject
        {
            ["status"] = "BLOCKED",
            ["reason"] = reason,
            ["requested"] = requested,
            ["accepted"] = accepted,
            ["next_step"] = nextStep
        };
    }

    private static JsonNode ClickByAutomationId(string automationId, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var element = mainWindow.FindFirstDescendant(cf.ByAutomationId(automationId))
            ?? throw new InvalidOperationException($"Element not found: {automationId}");

        // Try InvokePattern first
        if (element.Patterns.Invoke.TryGetPattern(out var invokePattern))
        {
            var savedForeground = JsonRpcHandler.Stealth ? GetForegroundWindow() : IntPtr.Zero;
            try
            {
                invokePattern.Invoke();
            }
            finally
            {
                if (JsonRpcHandler.Stealth && savedForeground != IntPtr.Zero)
                {
                    SetForegroundWindow(savedForeground);
                }
            }

            return new JsonObject
            {
                ["clicked"] = true,
                ["automationId"] = automationId,
                ["method"] = "InvokePattern"
            };
        }

        // Fallback to mouse click — use GetClickablePoint for robustness
        Point center;
        if (element.TryGetClickablePoint(out var clickable))
        {
            center = clickable;
        }
        else
        {
            var rect = element.BoundingRectangle;
            center = new Point(
                (int)(rect.X + rect.Width / 2),
                (int)(rect.Y + rect.Height / 2));
        }

        if (JsonRpcHandler.Stealth)
        {
            return FlashFocusClick(center.X, center.Y, mainWindow, automationId);
        }

        Mouse.Click(center);

        return new JsonObject
        {
            ["clicked"] = true,
            ["automationId"] = automationId,
            ["method"] = "MouseClick",
            ["x"] = center.X,
            ["y"] = center.Y
        };
    }

    private static JsonObject FlashFocusClick(
        int x,
        int y,
        AutomationElement? mainWindow,
        string? automationId = null)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var targetHwnd = mainWindow.Properties.NativeWindowHandle.ValueOrDefault;
        if (targetHwnd == IntPtr.Zero)
            throw new InvalidOperationException("Connected window has no native HWND");

        var savedForeground = GetForegroundWindow();
        var stopwatch = Stopwatch.StartNew();
        try
        {
            ShowWindow(targetHwnd, SW_RESTORE);
            var foregroundSet = SetForegroundWindow(targetHwnd);
            if (!foregroundSet || GetForegroundWindow() != targetHwnd)
            {
                throw new InvalidOperationException(
                    "flash-focus click could not activate the debuggee window safely");
            }
            Mouse.Click(new Point(x, y));
        }
        finally
        {
            if (savedForeground != IntPtr.Zero)
            {
                SetForegroundWindow(savedForeground);
            }
            stopwatch.Stop();
        }

        var result = new JsonObject
        {
            ["clicked"] = true,
            ["method"] = "flash-focus",
            ["x"] = x,
            ["y"] = y,
            ["flash_ms"] = (int)stopwatch.ElapsedMilliseconds
        };
        if (automationId is not null)
        {
            result["automationId"] = automationId;
        }
        return result;
    }

    private static void RejectStealthMouseInput(string commandName)
    {
        if (!JsonRpcHandler.Stealth)
        {
            return;
        }

        Program.Log($"stealth: blocking {commandName} coordinate mouse input");
        throw new InvalidOperationException(
            $"{commandName} is not available in stealth mode because it requires coordinate mouse input. "
            + "Use ui_click with automationId or ui_bring_to_front first.");
    }

    private static (int x, int y) GetCoordinates(JsonNode? @params)
    {
        var x = @params?["x"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing 'x' coordinate");
        var y = @params?["y"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing 'y' coordinate");
        return (x, y);
    }

    private static IReadOnlyList<Point> BuildDragWaypoints(int x1, int y1, int x2, int y2, int steps)
    {
        var deltaX = x2 - x1;
        var deltaY = y2 - y1;
        var distance = Math.Sqrt((deltaX * deltaX) + (deltaY * deltaY));

        var thresholdRatio = distance <= 0
            ? 1.0
            : Math.Min(1.0, DragThresholdPixels / distance);

        var thresholdX = x1 + (int)Math.Round(deltaX * thresholdRatio);
        var thresholdY = y1 + (int)Math.Round(deltaY * thresholdRatio);

        if (thresholdX == x1 && deltaX != 0)
        {
            thresholdX += Math.Sign(deltaX);
        }

        if (thresholdY == y1 && deltaY != 0)
        {
            thresholdY += Math.Sign(deltaY);
        }

        // Diagonal short-drag guard: rounding above can leave both axes
        // below DragThresholdPixels (e.g. (0,0)→(10,10) with threshold 4
        // produces first waypoint (4,4), which sits inside the WPF
        // rectreshold and skips DoDragDrop). Force the dominant axis out
        // past the threshold so the first move always crosses it.
        if (Math.Abs(thresholdX - x1) < DragThresholdPixels &&
            Math.Abs(thresholdY - y1) < DragThresholdPixels)
        {
            if (Math.Abs(deltaX) >= Math.Abs(deltaY) && deltaX != 0)
            {
                thresholdX = x1 + (Math.Sign(deltaX) * DragThresholdPixels);
            }
            else if (deltaY != 0)
            {
                thresholdY = y1 + (Math.Sign(deltaY) * DragThresholdPixels);
            }
        }

        var waypoints = new List<Point>(steps);
        for (var index = 1; index <= steps; index++)
        {
            if (index == 1)
            {
                waypoints.Add(new Point(thresholdX, thresholdY));
                continue;
            }

            var progress = (double)(index - 1) / (steps - 1);
            var currentX = thresholdX + (int)Math.Round((x2 - thresholdX) * progress);
            var currentY = thresholdY + (int)Math.Round((y2 - thresholdY) * progress);
            waypoints.Add(new Point(currentX, currentY));
        }

        if (waypoints.Count > 0)
        {
            waypoints[^1] = new Point(x2, y2);
        }

        return waypoints;
    }

    private static void EnsureForeground(AutomationElement? mainWindow)
    {
        if (JsonRpcHandler.Stealth)
        {
            Program.Log("stealth: skipping foreground");
            return;
        }

        if (mainWindow is null)
        {
            return;
        }

        var hwnd = mainWindow.Properties.NativeWindowHandle.ValueOrDefault;
        if (hwnd == IntPtr.Zero)
        {
            return;
        }

        var foregroundHwnd = GetForegroundWindow();
        var currentThread = GetCurrentThreadId();
        var foregroundThread = GetWindowThreadProcessId(foregroundHwnd, IntPtr.Zero);
        var targetThread = GetWindowThreadProcessId(hwnd, IntPtr.Zero);
        var attachedThreads = new List<uint>();

        try
        {
            foreach (var threadId in new HashSet<uint> { foregroundThread, targetThread })
            {
                if (threadId != 0 && threadId != currentThread && AttachThreadInput(currentThread, threadId, true))
                {
                    attachedThreads.Add(threadId);
                }
            }

            ShowWindow(hwnd, SW_RESTORE);
            BringWindowToTop(hwnd);
            SetForegroundWindow(hwnd);
            if (!WaitForForeground(hwnd))
            {
                Program.Log("foreground activation did not settle after first request; retrying once");
                ShowWindow(hwnd, SW_RESTORE);
                BringWindowToTop(hwnd);
                SetForegroundWindow(hwnd);
            }
        }
        finally
        {
            for (var index = attachedThreads.Count - 1; index >= 0; index--)
            {
                AttachThreadInput(currentThread, attachedThreads[index], false);
            }
        }

        if (!WaitForForeground(hwnd))
        {
            throw new InvalidOperationException(
                "coordinate mouse input could not activate the debuggee window safely");
        }
    }

    private static bool WaitForForeground(IntPtr hwnd)
    {
        var stopwatch = Stopwatch.StartNew();
        while (stopwatch.ElapsedMilliseconds < ForegroundActivationTimeoutMs)
        {
            if (GetForegroundWindow() == hwnd)
            {
                return true;
            }

            Thread.Sleep(ForegroundActivationPollMs);
        }

        return GetForegroundWindow() == hwnd;
    }
}
