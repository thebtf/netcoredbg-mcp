using System.Drawing;
using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.Core.Input;
using FlaUI.Core.WindowsAPI;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class SelectionCommands
{
    public static JsonNode MultiSelect(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var automationId = @params?["automationId"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: automationId");

        var indices = @params?["indices"]?.AsArray()
            ?? throw new ArgumentException("Missing required parameter: indices (array of int)");

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var container = mainWindow.FindFirstDescendant(cf.ByAutomationId(automationId))
            ?? throw new InvalidOperationException($"Container not found: {automationId}");

        var children = container.FindAllChildren();
        var selected = new JsonArray();
        var isFirst = true;

        foreach (var indexNode in indices)
        {
            var index = indexNode?.GetValue<int>()
                ?? throw new ArgumentException("Each index must be an integer");

            if (index < 0 || index >= children.Length)
                throw new ArgumentOutOfRangeException($"Index {index} out of range (0..{children.Length - 1})");

            var child = children[index];

            if (child.Patterns.SelectionItem.TryGetPattern(out var selectionPattern))
            {
                if (isFirst)
                    selectionPattern.Select();
                else
                    selectionPattern.AddToSelection();
            }
            else
            {
                // Fallback: Ctrl+Click
                var rect = child.BoundingRectangle;
                var center = new Point(
                    (int)(rect.X + rect.Width / 2),
                    (int)(rect.Y + rect.Height / 2));

                if (!isFirst)
                    Keyboard.Press(VirtualKeyShort.CONTROL);

                Mouse.Click(center);

                if (!isFirst)
                    Keyboard.Release(VirtualKeyShort.CONTROL);
            }

            selected.Add(index);
            isFirst = false;
        }

        return new JsonObject
        {
            ["selected"] = true,
            ["automationId"] = automationId,
            ["indices"] = selected
        };
    }

    public static JsonNode ExpandCollapse(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var automationId = @params?["automationId"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: automationId");
        var action = @params?["action"]?.GetValue<string>()?.ToLowerInvariant() ?? "toggle";

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var element = mainWindow.FindFirstDescendant(cf.ByAutomationId(automationId))
            ?? throw new InvalidOperationException($"Element not found: {automationId}");

        if (!element.Patterns.ExpandCollapse.TryGetPattern(out var expandCollapsePattern))
            throw new InvalidOperationException($"Element '{automationId}' does not support ExpandCollapsePattern");

        var previousState = expandCollapsePattern.ExpandCollapseState.Value;

        switch (action)
        {
            case "expand":
                expandCollapsePattern.Expand();
                break;
            case "collapse":
                expandCollapsePattern.Collapse();
                break;
            case "toggle":
                if (previousState == ExpandCollapseState.Expanded)
                    expandCollapsePattern.Collapse();
                else
                    expandCollapsePattern.Expand();
                break;
            default:
                throw new ArgumentException($"Unknown action: {action}. Use 'expand', 'collapse', or 'toggle'.");
        }

        var newState = expandCollapsePattern.ExpandCollapseState.Value;

        return new JsonObject
        {
            ["done"] = true,
            ["automationId"] = automationId,
            ["previousState"] = previousState.ToString(),
            ["currentState"] = newState.ToString()
        };
    }
}
