using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Input;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class PatternCommands
{
    public static JsonNode InvokeElement(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var searchRoot = ElementCommands.ResolveSearchRoot(mainWindow, @params, automation);
        var element = ElementCommands.FindElementCascade(searchRoot, @params, automation);

        string method;
        if (element.Patterns.Invoke.IsSupported)
        {
            element.Patterns.Invoke.Pattern.Invoke();
            method = "InvokePattern";
        }
        else
        {
            element.Click();
            method = "Click";
        }

        Wait.UntilInputIsProcessed();

        return new JsonObject
        {
            ["invoked"] = true,
            ["method"] = method,
            ["automationId"] = element.AutomationId,
            ["name"] = element.Name,
            ["controlType"] = element.ControlType.ToString()
        };
    }

    public static JsonNode ToggleElement(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var searchRoot = ElementCommands.ResolveSearchRoot(mainWindow, @params, automation);
        var element = ElementCommands.FindElementCascade(searchRoot, @params, automation);

        if (!element.Patterns.Toggle.IsSupported)
        {
            // List supported patterns for diagnostic help
            var patterns = new List<string>();
            try
            {
                var supported = element.GetSupportedPatterns();
                foreach (var p in supported)
                    patterns.Add(p.Name);
            }
            catch
            {
                // Ignore pattern enumeration failure
            }

            var patternList = patterns.Count > 0
                ? string.Join(", ", patterns)
                : "none detected";

            throw new InvalidOperationException(
                $"Element does not support TogglePattern. " +
                $"Element: {element.ControlType} '{element.Name}' (id='{element.AutomationId}'). " +
                $"Supported patterns: {patternList}");
        }

        element.Patterns.Toggle.Pattern.Toggle();
        Wait.UntilInputIsProcessed();

        var newState = element.Patterns.Toggle.Pattern.ToggleState.ValueOrDefault;

        return new JsonObject
        {
            ["toggled"] = true,
            ["newState"] = newState.ToString(),
            ["automationId"] = element.AutomationId,
            ["name"] = element.Name,
            ["controlType"] = element.ControlType.ToString()
        };
    }
}
