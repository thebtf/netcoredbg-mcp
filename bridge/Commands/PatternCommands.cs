using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
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

        // Use BuildElementInfo so every property read is try/catch-wrapped.
        // Direct access here would throw on WPF modal elements with
        // unsupported properties (e.g. ClassName #30012) AFTER the invoke
        // already succeeded, reporting error for a completed action.
        var info = ElementCommands.BuildElementInfo(element, includePatterns: false);
        info["invoked"] = true;
        info["method"] = method;
        return info;
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

            // Property accesses may throw on uncooperative UIA providers, so
            // build a defensive description instead of interpolating directly.
            string safeAid, safeName, safeCtrl;
            try { safeAid = element.AutomationId ?? ""; } catch { safeAid = ""; }
            try { safeName = element.Name ?? ""; } catch { safeName = ""; }
            try { safeCtrl = element.ControlType.ToString(); } catch { safeCtrl = "?"; }

            throw new InvalidOperationException(
                $"Element does not support TogglePattern. " +
                $"Element: {safeCtrl} '{safeName}' (id='{safeAid}'). " +
                $"Supported patterns: {patternList}");
        }

        element.Patterns.Toggle.Pattern.Toggle();
        Wait.UntilInputIsProcessed();

        var newState = element.Patterns.Toggle.Pattern.ToggleState.ValueOrDefault;

        var info = ElementCommands.BuildElementInfo(element, includePatterns: false);
        info["toggled"] = true;
        info["newState"] = newState.ToString();
        return info;
    }

    public static JsonNode ExpandElement(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var automationId = @params?["automationId"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: automationId");

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var element = mainWindow.FindFirstDescendant(cf.ByAutomationId(automationId))
            ?? throw new InvalidOperationException($"Element not found: {automationId}");

        if (!element.Patterns.ExpandCollapse.TryGetPattern(out var pattern))
            throw new InvalidOperationException(
                $"Element '{automationId}' does not support ExpandCollapsePattern");

        var currentState = pattern.ExpandCollapseState.Value;
        var wasAlready = currentState == ExpandCollapseState.Expanded;

        if (!wasAlready)
        {
            pattern.Expand();
        }

        Program.Log($"expand: '{automationId}' expanded (wasAlready={wasAlready})");

        return new JsonObject
        {
            ["expanded"] = true,
            ["automation_id"] = automationId,
            ["was_already"] = wasAlready
        };
    }

    public static JsonNode CollapseElement(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var automationId = @params?["automationId"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: automationId");

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var element = mainWindow.FindFirstDescendant(cf.ByAutomationId(automationId))
            ?? throw new InvalidOperationException($"Element not found: {automationId}");

        if (!element.Patterns.ExpandCollapse.TryGetPattern(out var pattern))
            throw new InvalidOperationException(
                $"Element '{automationId}' does not support ExpandCollapsePattern");

        var currentState = pattern.ExpandCollapseState.Value;
        var wasAlready = currentState == ExpandCollapseState.Collapsed;

        if (!wasAlready)
        {
            pattern.Collapse();
        }

        Program.Log($"collapse: '{automationId}' collapsed (wasAlready={wasAlready})");

        return new JsonObject
        {
            ["collapsed"] = true,
            ["automation_id"] = automationId,
            ["was_already"] = wasAlready
        };
    }

    public static JsonNode SetRangeValue(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var automationId = @params?["automationId"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: automationId");

        // Accept value as double; handle both integer and floating-point JSON representations.
        double value;
        var valueNode = @params?["value"]
            ?? throw new ArgumentException("Missing required parameter: value");

        try
        {
            value = valueNode.GetValue<double>();
        }
        catch
        {
            throw new ArgumentException($"Parameter 'value' must be a number, got: {valueNode}");
        }

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var element = mainWindow.FindFirstDescendant(cf.ByAutomationId(automationId))
            ?? throw new InvalidOperationException($"Element not found: {automationId}");

        if (!element.Patterns.RangeValue.TryGetPattern(out var pattern))
            throw new InvalidOperationException(
                $"Element '{automationId}' does not support RangeValuePattern");

        var minimum = pattern.Minimum.Value;
        var maximum = pattern.Maximum.Value;

        if (value < minimum || value > maximum)
        {
            Program.Log($"set_value: {value} out of range [{minimum}..{maximum}] for '{automationId}'");
            return new JsonObject
            {
                ["set"] = false,
                ["reason"] = $"value {value} out of range [{minimum}..{maximum}]",
                ["automation_id"] = automationId,
                ["minimum"] = minimum,
                ["maximum"] = maximum
            };
        }

        pattern.SetValue(value);
        Program.Log($"set_value: '{automationId}' = {value} (range [{minimum}..{maximum}])");

        return new JsonObject
        {
            ["set"] = true,
            ["automation_id"] = automationId,
            ["value"] = value,
            ["minimum"] = minimum,
            ["maximum"] = maximum
        };
    }
}
