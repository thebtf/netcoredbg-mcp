using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.Core.Input;
using FlaUI.Core.Patterns;
using FlaUI.Core.WindowsAPI;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class ListCommands
{
    public static JsonNode InvokeItem(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var listElement = ResolveList(@params, automation, mainWindow);
        var itemElement = ResolveListItem(listElement, @params, automation);
        var invoke = @params?["invoke"]?.GetValue<string>() ?? "default";
        var method = InvokeResolvedItem(itemElement, invoke);
        var info = ElementCommands.BuildElementInfo(itemElement, includePatterns: false);
        info["status"] = "PASS";
        info["invoked"] = true;
        info["method"] = method;
        return info;
    }

    public static JsonNode ToggleItemChild(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var listElement = ResolveList(@params, automation, mainWindow);
        var itemElement = ResolveListItem(listElement, @params, automation);
        var child = @params?["child"] as JsonObject
            ?? throw new ArgumentException("Missing required parameter: child");
        var childElement = ResolveChild(itemElement, child, automation);

        if (!childElement.Patterns.Toggle.TryGetPattern(out var togglePattern))
            throw new InvalidOperationException("List item child does not support TogglePattern");

        var targetState = @params?["targetState"]?.GetValue<string>();
        var before = togglePattern.ToggleState.ValueOrDefault.ToString();
        var after = before;
        var attempts = 0;
        const int maxAttempts = 10;

        if (string.IsNullOrWhiteSpace(targetState))
        {
            togglePattern.Toggle();
            attempts = 1;
            after = togglePattern.ToggleState.ValueOrDefault.ToString();
        }
        else
        {
            while (!string.Equals(after, targetState, StringComparison.OrdinalIgnoreCase) &&
                   attempts < maxAttempts)
            {
                togglePattern.Toggle();
                attempts += 1;
                after = togglePattern.ToggleState.ValueOrDefault.ToString();
            }
        }
        var info = ElementCommands.BuildElementInfo(childElement, includePatterns: false);
        info["status"] = string.IsNullOrWhiteSpace(targetState) ||
            string.Equals(after, targetState, StringComparison.OrdinalIgnoreCase)
                ? "PASS"
                : "FAIL";
        info["toggled"] = !string.Equals(before, after, StringComparison.Ordinal);
        info["old_state"] = before;
        info["new_state"] = after;
        info["target_state"] = targetState;
        info["attempts"] = attempts;
        info["item"] = ElementCommands.BuildElementInfo(itemElement, includePatterns: false);
        return info;
    }

    private static AutomationElement ResolveList(
        JsonNode? @params,
        UIA3Automation automation,
        AutomationElement mainWindow)
    {
        var selector = @params?["selector"] as JsonObject
            ?? throw new ArgumentException("Missing required parameter: selector");
        var root = ElementCommands.ResolveSearchRoot(mainWindow, selector, automation);
        if (!selector.ContainsKey("controlType"))
            selector["controlType"] = "List";
        return ElementCommands.FindElementCascade(root, selector, automation);
    }

    private static AutomationElement ResolveListItem(
        AutomationElement listElement,
        JsonNode? @params,
        UIA3Automation automation)
    {
        var item = @params?["item"] as JsonObject ?? new JsonObject();
        var itemIndex = ReadOptionalInt(@params?["itemIndex"]);
        if (itemIndex is not null)
        {
            var cf = new ConditionFactory(automation.PropertyLibrary);
            var items = listElement.FindAllDescendants(new OrCondition(
                cf.ByControlType(ControlType.ListItem),
                cf.ByControlType(ControlType.DataItem)));
            if (itemIndex.Value < 0 || itemIndex.Value >= items.Length)
                throw new ArgumentException($"List item index out of range: {itemIndex.Value}");
            return items[itemIndex.Value];
        }

        if (!item.ContainsKey("controlType"))
            item["controlType"] = "ListItem";
        return ElementCommands.FindElementCascade(listElement, item, automation);
    }

    private static AutomationElement ResolveChild(
        AutomationElement itemElement,
        JsonObject child,
        UIA3Automation automation)
    {
        var cf = new ConditionFactory(automation.PropertyLibrary);
        var conditions = new List<ConditionBase>();
        var automationId = child["automationId"]?.GetValue<string>();
        if (!string.IsNullOrWhiteSpace(automationId))
            conditions.Add(cf.ByAutomationId(automationId));
        var name = child["name"]?.GetValue<string>();
        if (!string.IsNullOrWhiteSpace(name))
            conditions.Add(cf.ByName(name));
        var controlType = child["controlType"]?.GetValue<string>();
        if (!string.IsNullOrWhiteSpace(controlType) &&
            Enum.TryParse<ControlType>(controlType, true, out var ct))
            conditions.Add(cf.ByControlType(ct));
        if (conditions.Count == 0)
            throw new ArgumentException("Child selector requires automationId, name, or controlType");

        var condition = conditions.Count == 1
            ? conditions[0]
            : new AndCondition(conditions.ToArray());
        return itemElement.FindFirstDescendant(condition)
            ?? throw new InvalidOperationException("List item child not found.");
    }

    private static string InvokeResolvedItem(AutomationElement itemElement, string invoke)
    {
        if (itemElement.Patterns.SelectionItem.TryGetPattern(out var selectionItem))
            selectionItem.Select();

        if (!string.Equals(invoke, "enter", StringComparison.OrdinalIgnoreCase) &&
            itemElement.Patterns.Invoke.TryGetPattern(out var invokePattern))
        {
            invokePattern.Invoke();
            return "InvokePattern";
        }

        itemElement.Focus();
        KeySequenceCommands.SendSignedKeyDown(VirtualKeyShort.RETURN);
        KeySequenceCommands.SendSignedKeyUp(VirtualKeyShort.RETURN);
        return "Keyboard.Enter";
    }

    private static int? ReadOptionalInt(JsonNode? node)
    {
        if (node is null)
            return null;
        try { return node.GetValue<int>(); }
        catch { return null; }
    }
}
