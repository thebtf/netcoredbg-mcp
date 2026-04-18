using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

/// <summary>
/// Shared helper for resolving a target top-level window from JSON-RPC parameters.
/// Used by both WindowCommands and TransformCommands to avoid duplicating the
/// process-scoped window search logic.
/// </summary>
internal static class WindowResolver
{
    /// <summary>
    /// Resolve the target top-level window from the given parameters.
    /// If <c>window_title</c> is present, searches all direct children of the desktop
    /// belonging to the connected process (filtered by ProcessId to avoid scanning
    /// every window on the desktop). Otherwise returns <paramref name="mainWindow"/>.
    /// </summary>
    /// <param name="params">The JSON-RPC parameter node. May be null.</param>
    /// <param name="automation">The active UIA3 automation instance.</param>
    /// <param name="mainWindow">The main window captured at connect-time. May be null.</param>
    /// <returns>The resolved AutomationElement for the target window.</returns>
    /// <exception cref="InvalidOperationException">
    /// Thrown when no matching window is found or the bridge is not connected.
    /// </exception>
    internal static AutomationElement Resolve(
        JsonNode? @params,
        UIA3Automation automation,
        AutomationElement? mainWindow)
    {
        var windowTitle = @params?["window_title"]?.GetValue<string>();

        if (windowTitle is not null)
        {
            // Search only among children of the desktop that belong to the
            // connected process. Using ByProcessId avoids walking every
            // top-level window on the desktop (avoids the dangerous full-
            // desktop FindFirst scan).
            var processId = JsonRpcHandler.ProcessId;
            if (processId > 0)
            {
                var cf = new ConditionFactory(automation.PropertyLibrary);
                var desktop = automation.GetDesktop();
                var windows = desktop.FindAll(TreeScope.Children, cf.ByProcessId(processId));

                var matches = new List<(AutomationElement Element, string Title)>();
                foreach (var child in windows)
                {
                    try
                    {
                        var name = child.Name ?? "";
                        if (name.Contains(windowTitle, StringComparison.OrdinalIgnoreCase))
                            matches.Add((child, name));
                    }
                    catch
                    {
                        // Ignore inaccessible windows during enumeration
                    }
                }

                if (matches.Count == 1)
                    return matches[0].Element;

                if (matches.Count > 1)
                {
                    // Ambiguous — multiple windows match the title fragment. List candidates
                    // so the caller can supply a more precise title or use the process ID directly.
                    var candidates = string.Join("; ", matches.Select(
                        m =>
                        {
                            var pid = -1;
                            try { pid = m.Element.Properties.ProcessId.ValueOrDefault; } catch { }
                            return $"'{m.Title}' (pid={pid})";
                        }));
                    throw new InvalidOperationException(
                        $"Ambiguous window_title '{windowTitle}': {matches.Count} windows match — {candidates}. "
                        + "Provide a more specific title or omit window_title to use the main window.");
                }
            }

            throw new InvalidOperationException(
                $"No window with title containing '{windowTitle}' found for the connected process.");
        }

        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        return mainWindow;
    }

    /// <summary>
    /// Safely read the UIA Name of an element; returns empty string if inaccessible.
    /// </summary>
    internal static string SafeGetTitle(AutomationElement element)
    {
        try { return element.Name ?? ""; }
        catch { return ""; }
    }
}
