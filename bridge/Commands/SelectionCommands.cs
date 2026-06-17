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

        var targets = SelectionTargets(container, automation);
        var selected = new JsonArray();
        var isFirst = true;
        var usedPattern = false;
        var usedClickFallback = false;

        foreach (var indexNode in indices)
        {
            var index = indexNode?.GetValue<int>()
                ?? throw new ArgumentException("Each index must be an integer");

            if (index < 0 || index >= targets.Length)
                throw new ArgumentOutOfRangeException($"Index {index} out of range (0..{targets.Length - 1})");

            var child = targets[index];

            if (child.Patterns.SelectionItem.TryGetPattern(out var selectionPattern))
            {
                if (isFirst)
                    selectionPattern.Select();
                else
                    selectionPattern.AddToSelection();
                usedPattern = true;
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

                try
                {
                    Mouse.Click(center);
                }
                finally
                {
                    if (!isFirst)
                        Keyboard.Release(VirtualKeyShort.CONTROL);
                }
                usedClickFallback = true;
            }

            selected.Add(index);
            isFirst = false;
        }

        return new JsonObject
        {
            ["selected"] = true,
            ["selected_count"] = selected.Count,
            ["automationId"] = automationId,
            ["indices"] = selected,
            ["method"] = usedClickFallback
                ? (usedPattern ? "Mixed" : "ClickFallback")
                : "SelectionItemPattern"
        };
    }

    public static JsonNode GetSelectedItem(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var searchRoot = ElementCommands.ResolveSearchRoot(mainWindow, @params, automation);
        var container = ElementCommands.FindElementCascade(searchRoot, @params, automation);
        var (selectedItems, method) = SelectedItems(container, automation);

        if (selectedItems.Length == 0)
        {
            return new JsonObject
            {
                ["index"] = -1,
                ["name"] = "",
                ["automationId"] = "",
                ["controlType"] = "",
                ["selected"] = false,
                ["selected_count"] = 0,
                ["method"] = method
            };
        }

        var selectedItem = selectedItems[0];
        var result = ElementCommands.BuildElementInfo(selectedItem, includePatterns: false);
        result["index"] = IndexOfTarget(container, automation, selectedItem);
        result["selected"] = true;
        result["selected_count"] = selectedItems.Length;
        result["method"] = method;
        return result;
    }

    private static (AutomationElement[] Items, string Method) SelectedItems(
        AutomationElement container,
        UIA3Automation automation)
    {
        try
        {
            if (container.Patterns.Selection.TryGetPattern(out var selectionPattern))
            {
                var selected = selectionPattern.Selection.ValueOrDefault ?? Array.Empty<AutomationElement>();
                if (selected.Length > 0)
                    return (selected, "SelectionPattern");
            }
        }
        catch { /* unsupported */ }

        var selectedTargets = SelectionTargets(container, automation)
            .Where(IsSelected)
            .ToArray();
        return (selectedTargets, "SelectionItemPatternScan");
    }

    private static bool IsSelected(AutomationElement element)
    {
        try
        {
            return element.Patterns.SelectionItem.TryGetPattern(out var pattern) &&
                   pattern.IsSelected.Value;
        }
        catch
        {
            return false;
        }
    }

    private static int IndexOfTarget(
        AutomationElement container,
        UIA3Automation automation,
        AutomationElement target)
    {
        var targets = SelectionTargets(container, automation);
        for (var index = 0; index < targets.Length; index++)
        {
            if (SameRuntimeId(targets[index], target))
                return index;
        }
        return -1;
    }

    private static bool SameRuntimeId(AutomationElement left, AutomationElement right)
    {
        var leftId = RuntimeId(left);
        var rightId = RuntimeId(right);
        return leftId is not null &&
               rightId is not null &&
               leftId.SequenceEqual(rightId);
    }

    private static int[]? RuntimeId(AutomationElement element)
    {
        try { return element.Properties.RuntimeId.ValueOrDefault; }
        catch { return null; }
    }

    private static AutomationElement[] SelectionTargets(AutomationElement container, UIA3Automation automation)
    {
        var rowTargets = container.FindAllChildren(RowCondition(automation))
            .Where(IsRowLike)
            .ToArray();
        if (rowTargets.Length > 0)
            return rowTargets;

        var selectableChildren = container.FindAllChildren()
            .Where(IsSelectableTarget)
            .ToArray();
        if (selectableChildren.Length > 0)
            return selectableChildren;

        var rowDescendants = container.FindAllDescendants(RowCondition(automation))
            .Where(IsRowLike)
            .ToArray();
        return rowDescendants.Length > 0 ? rowDescendants : container.FindAllChildren();
    }

    private static ConditionBase RowCondition(UIA3Automation automation)
    {
        var cf = new ConditionFactory(automation.PropertyLibrary);
        return new OrCondition(
            cf.ByControlType(ControlType.DataItem),
            cf.ByControlType(ControlType.Custom),
            cf.ByControlType(ControlType.ListItem));
    }

    private static bool IsSelectableTarget(AutomationElement element)
    {
        return element.Patterns.SelectionItem.TryGetPattern(out _);
    }

    private static bool IsRowLike(AutomationElement element)
    {
        var controlType = element.ControlType;
        return controlType == ControlType.DataItem ||
               controlType == ControlType.Custom ||
               controlType == ControlType.ListItem;
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
