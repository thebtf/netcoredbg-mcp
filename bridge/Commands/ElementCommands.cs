using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class ElementCommands
{
    public static JsonNode Connect(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var pid = @params?["pid"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing required parameter: pid");

        var desktop = automation.GetDesktop();
        var windows = desktop.FindAllChildren(
            new ConditionFactory(automation.PropertyLibrary)
                .ByProcessId(pid));

        if (windows.Length == 0)
            throw new InvalidOperationException($"No window found for process {pid}");

        var window = windows[0];
        JsonRpcHandler.MainWindow = window;

        Program.Log($"Connected to window: {window.Name} (pid={pid})");

        return new JsonObject
        {
            ["connected"] = true,
            ["title"] = window.Name
        };
    }

    public static JsonNode FindElement(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var conditions = new List<ConditionBase>();

        var automationId = @params?["automationId"]?.GetValue<string>();
        if (automationId is not null)
            conditions.Add(cf.ByAutomationId(automationId));

        var name = @params?["name"]?.GetValue<string>();
        if (name is not null)
            conditions.Add(cf.ByName(name));

        var controlType = @params?["controlType"]?.GetValue<string>();
        if (controlType is not null && Enum.TryParse<ControlType>(controlType, true, out var ct))
            conditions.Add(cf.ByControlType(ct));

        if (conditions.Count == 0)
            throw new ArgumentException("At least one search criterion required: automationId, name, or controlType");

        var condition = conditions.Count == 1
            ? conditions[0]
            : new AndCondition(conditions.ToArray());

        var element = mainWindow.FindFirstDescendant(condition);
        if (element is null)
            return new JsonObject { ["found"] = false };

        return BuildElementInfo(element);
    }

    public static JsonNode GetTree(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var maxDepth = @params?["maxDepth"]?.GetValue<int>() ?? 3;
        var maxChildren = @params?["maxChildren"]?.GetValue<int>() ?? 25;

        return BuildTree(mainWindow, maxDepth, maxChildren, 0);
    }

    private static JsonNode BuildTree(AutomationElement element, int maxDepth, int maxChildren, int currentDepth)
    {
        var node = BuildElementInfo(element);

        if (currentDepth >= maxDepth)
            return node;

        var children = element.FindAllChildren();
        var childArray = new JsonArray();
        var count = Math.Min(children.Length, maxChildren);

        for (var i = 0; i < count; i++)
        {
            childArray.Add(BuildTree(children[i], maxDepth, maxChildren, currentDepth + 1));
        }

        if (children.Length > maxChildren)
            childArray.Add(new JsonObject { ["truncated"] = true, ["total"] = children.Length });

        node["children"] = childArray;
        return node;
    }

    private static JsonObject BuildElementInfo(AutomationElement element)
    {
        var rect = element.BoundingRectangle;
        var patterns = new JsonArray();

        try
        {
            var supported = element.GetSupportedPatterns();
            foreach (var p in supported)
                patterns.Add(p.Name);
        }
        catch
        {
            // Some elements may not support pattern enumeration
        }

        return new JsonObject
        {
            ["found"] = true,
            ["automationId"] = element.AutomationId,
            ["name"] = element.Name,
            ["controlType"] = element.ControlType.ToString(),
            ["className"] = element.ClassName,
            ["rect"] = new JsonObject
            {
                ["x"] = rect.X,
                ["y"] = rect.Y,
                ["width"] = rect.Width,
                ["height"] = rect.Height
            },
            ["patterns"] = patterns
        };
    }
}
