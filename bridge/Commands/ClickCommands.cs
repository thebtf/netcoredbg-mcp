using System.Drawing;
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
            Mouse.Click(new Point(x.Value, y.Value));
            return new JsonObject { ["clicked"] = true, ["x"] = x.Value, ["y"] = y.Value };
        }

        throw new ArgumentException("Provide 'automationId' or 'x'/'y' coordinates");
    }

    public static JsonNode RightClick(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var (x, y) = GetCoordinates(@params);
        Mouse.RightClick(new Point(x, y));
        return new JsonObject { ["rightClicked"] = true, ["x"] = x, ["y"] = y };
    }

    public static JsonNode DoubleClick(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var (x, y) = GetCoordinates(@params);
        Mouse.DoubleClick(new Point(x, y));
        return new JsonObject { ["doubleClicked"] = true, ["x"] = x, ["y"] = y };
    }

    public static JsonNode Drag(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var fromX = @params?["fromX"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing 'fromX'");
        var fromY = @params?["fromY"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing 'fromY'");
        var toX = @params?["toX"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing 'toX'");
        var toY = @params?["toY"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing 'toY'");
        var steps = @params?["steps"]?.GetValue<int>() ?? 10;

        var from = new Point(fromX, fromY);
        var to = new Point(toX, toY);

        Mouse.MoveTo(from);
        Mouse.Down(MouseButton.Left);

        for (var i = 1; i <= steps; i++)
        {
            var progress = (double)i / steps;
            var currentX = (int)(fromX + (toX - fromX) * progress);
            var currentY = (int)(fromY + (toY - fromY) * progress);
            Mouse.MoveTo(new Point(currentX, currentY));
            Thread.Sleep(10);
        }

        Mouse.Up(MouseButton.Left);

        return new JsonObject
        {
            ["dragged"] = true,
            ["from"] = new JsonObject { ["x"] = fromX, ["y"] = fromY },
            ["to"] = new JsonObject { ["x"] = toX, ["y"] = toY }
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
}
