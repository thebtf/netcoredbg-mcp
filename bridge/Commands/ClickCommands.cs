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
    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    private const int SW_RESTORE = 9;
    private const int DragThresholdPixels = 5;

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
            EnsureForeground(mainWindow);
            Mouse.Click(new Point(x.Value, y.Value));
            return new JsonObject { ["clicked"] = true, ["x"] = x.Value, ["y"] = y.Value };
        }

        throw new ArgumentException("Provide 'automationId' or 'x'/'y' coordinates");
    }

    public static JsonNode RightClick(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var (x, y) = GetCoordinates(@params);
        EnsureForeground(mainWindow);
        Mouse.RightClick(new Point(x, y));
        return new JsonObject { ["rightClicked"] = true, ["x"] = x, ["y"] = y };
    }

    public static JsonNode DoubleClick(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var (x, y) = GetCoordinates(@params);
        EnsureForeground(mainWindow);
        Mouse.DoubleClick(new Point(x, y));
        return new JsonObject { ["doubleClicked"] = true, ["x"] = x, ["y"] = y };
    }

    public static JsonNode Drag(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
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

        try
        {
            foreach (var modifier in temporaryModifiers)
            {
                Keyboard.Press(modifier);
                pressedTemporaryModifiers.Add(modifier);
            }

            Mouse.MoveTo(new Point(x1, y1));
            Mouse.Down(MouseButton.Left);
            mouseButtonDown = true;

            var waypoints = BuildDragWaypoints(x1, y1, x2, y2, steps);
            var delayMs = Math.Max(1, (int)Math.Round(speedMs / (double)steps));

            foreach (var waypoint in waypoints)
            {
                Mouse.MoveTo(waypoint);
                Thread.Sleep(delayMs);
            }
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
                    Mouse.Up(MouseButton.Left);
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
            ["steps"] = steps,
            ["duration_ms"] = stopwatch.ElapsedMilliseconds
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
            invokePattern.Invoke();
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
        if (mainWindow is null)
        {
            return;
        }

        var hwnd = mainWindow.Properties.NativeWindowHandle.ValueOrDefault;
        if (hwnd == IntPtr.Zero)
        {
            return;
        }

        ShowWindow(hwnd, SW_RESTORE);
        SetForegroundWindow(hwnd);
    }
}
