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
}
