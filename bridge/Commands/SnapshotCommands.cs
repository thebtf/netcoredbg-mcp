using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class SnapshotCommands
{
    private static readonly HashSet<string> AllowedFields = new(StringComparer.OrdinalIgnoreCase)
    {
        "focus",
        "selection",
        "value",
        "text",
        "enabled",
        "visible",
        "window",
    };

    public static JsonNode UiQuery(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var selector = @params?["selector"] as JsonObject ?? new JsonObject();
        var fields = ReadFields(@params);
        var maxResults = Math.Max(1, Math.Min(@params?["maxResults"]?.GetValue<int>() ?? 20, 20));
        var elements = ResolveElements(selector, automation, mainWindow, maxResults + 1);
        var returned = elements.Take(maxResults).ToList();
        var records = new JsonArray();

        foreach (var element in returned)
            records.Add(BuildRecord(element, fields, automation, mainWindow));

        return new JsonObject
        {
            ["status"] = "PASS",
            ["fields"] = ToArray(fields),
            ["element_count"] = elements.Count,
            ["returned_count"] = records.Count,
            ["omitted_count"] = Math.Max(0, elements.Count - records.Count),
            ["elements"] = records,
        };
    }

    private static List<AutomationElement> ResolveElements(
        JsonObject selector,
        UIA3Automation automation,
        AutomationElement mainWindow,
        int maxResults)
    {
        var root = ElementCommands.ResolveSearchRoot(mainWindow, selector, automation);
        if (HasSelector(selector))
            return new List<AutomationElement>
            {
                ElementCommands.FindElementCascade(root, selector, automation),
            };

        var elements = new List<AutomationElement> { root };
        foreach (var child in root.FindAllDescendants().Take(Math.Max(0, maxResults - 1)))
            elements.Add(child);
        return elements;
    }

    private static bool HasSelector(JsonObject selector)
    {
        return selector.ContainsKey("automationId") ||
               selector.ContainsKey("name") ||
               selector.ContainsKey("controlType") ||
               selector.ContainsKey("xpath");
    }

    private static List<string> ReadFields(JsonNode? @params)
    {
        var fieldsNode = @params?["fields"] as JsonArray;
        if (fieldsNode is null || fieldsNode.Count == 0)
            return new List<string> { "focus", "selection", "text" };

        var fields = new List<string>();
        foreach (var node in fieldsNode)
        {
            var field = node?.GetValue<string>() ?? "";
            if (!AllowedFields.Contains(field))
                throw new ArgumentException($"Unknown UI field: {field}");
            fields.Add(field);
        }
        return fields;
    }

    private static JsonObject BuildRecord(
        AutomationElement element,
        List<string> fields,
        UIA3Automation automation,
        AutomationElement mainWindow)
    {
        var record = new JsonObject
        {
            ["element_id"] = ElementId(element),
        };
        foreach (var field in fields)
        {
            switch (field)
            {
                case "focus":
                    record["focus"] = IsFocused(element, automation);
                    break;
                case "selection":
                    record["selection"] = SelectionState(element);
                    break;
                case "value":
                    record["value"] = ValueState(element);
                    break;
                case "text":
                    record["text"] = TextState(element);
                    break;
                case "enabled":
                    record["enabled"] = SafeBool(() => element.Properties.IsEnabled.ValueOrDefault);
                    break;
                case "visible":
                    record["visible"] = !SafeBool(() => element.Properties.IsOffscreen.ValueOrDefault);
                    break;
                case "window":
                    record["window"] = WindowState(mainWindow);
                    break;
            }
        }
        return record;
    }

    private static JsonObject SelectionState(AutomationElement element)
    {
        try
        {
            if (element.Patterns.SelectionItem.TryGetPattern(out var pattern))
                return new JsonObject { ["selected"] = pattern.IsSelected.Value };
        }
        catch { /* unsupported */ }
        return new JsonObject { ["supported"] = false };
    }

    private static string? ValueState(AutomationElement element)
    {
        try
        {
            if (element.Patterns.Value.TryGetPattern(out var pattern))
                return pattern.Value.ValueOrDefault;
        }
        catch { /* unsupported */ }
        return null;
    }

    private static string TextState(AutomationElement element)
    {
        try
        {
            if (element.Patterns.Text.TryGetPattern(out var pattern))
                return pattern.DocumentRange.GetText(-1) ?? "";
        }
        catch { /* fallback to name */ }
        return SafeString(() => element.Name);
    }

    private static JsonObject WindowState(AutomationElement mainWindow)
    {
        return new JsonObject
        {
            ["title"] = SafeString(() => mainWindow.Name),
            ["automation_id"] = SafeString(() => mainWindow.AutomationId),
        };
    }

    private static bool IsFocused(AutomationElement element, UIA3Automation automation)
    {
        try
        {
            var focused = automation.FocusedElement();
            if (focused is null)
                return false;
            return ElementId(focused) == ElementId(element);
        }
        catch
        {
            return false;
        }
    }

    private static string ElementId(AutomationElement element)
    {
        var automationId = SafeString(() => element.AutomationId);
        if (!string.IsNullOrWhiteSpace(automationId))
            return automationId;
        var name = SafeString(() => element.Name);
        if (!string.IsNullOrWhiteSpace(name))
            return $"{SafeString(() => element.ControlType.ToString())}:{name}";
        return SafeString(() => element.ControlType.ToString());
    }

    private static JsonArray ToArray(IEnumerable<string> values)
    {
        var array = new JsonArray();
        foreach (var value in values)
            array.Add(value);
        return array;
    }

    private static string SafeString(Func<string?> read)
    {
        try { return read() ?? ""; }
        catch { return ""; }
    }

    private static bool SafeBool(Func<bool> read)
    {
        try { return read(); }
        catch { return false; }
    }
}
