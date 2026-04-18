using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class WindowCommands
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    private const int SW_RESTORE = 9;

    private static void EnsureForeground(AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            return;

        var hwnd = mainWindow.Properties.NativeWindowHandle.ValueOrDefault;
        if (hwnd == IntPtr.Zero)
            return;

        ShowWindow(hwnd, SW_RESTORE);
        SetForegroundWindow(hwnd);
    }

    private static AutomationElement ResolveTargetWindow(
        JsonNode? @params,
        UIA3Automation automation,
        AutomationElement? mainWindow)
    {
        var windowTitle = @params?["window_title"]?.GetValue<string>();

        if (windowTitle is not null)
        {
            // Search for a top-level window by title among all windows of the
            // connected process. We iterate top-level windows from the desktop.
            var processId = JsonRpcHandler.ProcessId;
            if (processId > 0)
            {
                var desktop = automation.GetDesktop();
                var children = desktop.FindAllChildren();
                foreach (var child in children)
                {
                    try
                    {
                        if (child.Properties.ProcessId.ValueOrDefault != processId)
                            continue;

                        var name = child.Name ?? "";
                        if (name.Contains(windowTitle, StringComparison.OrdinalIgnoreCase))
                            return child;
                    }
                    catch
                    {
                        // Ignore inaccessible windows
                    }
                }
            }

            throw new InvalidOperationException(
                $"No window with title containing '{windowTitle}' found for the connected process.");
        }

        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        return mainWindow;
    }

    private static string SafeGetWindowTitle(AutomationElement element)
    {
        try { return element.Name ?? ""; }
        catch { return ""; }
    }

    public static JsonNode CloseWindow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var target = ResolveTargetWindow(@params, automation, mainWindow);
        var title = SafeGetWindowTitle(target);

        if (!target.Patterns.Window.TryGetPattern(out var pattern))
            throw new InvalidOperationException("Element does not support WindowPattern");

        pattern.Close();
        Program.Log($"close_window: closed '{title}'");

        return new JsonObject
        {
            ["closed"] = true,
            ["window_title"] = title
        };
    }

    public static JsonNode MaximizeWindow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var target = ResolveTargetWindow(@params, automation, mainWindow);
        var title = SafeGetWindowTitle(target);

        if (!target.Patterns.Window.TryGetPattern(out var pattern))
            throw new InvalidOperationException("Element does not support WindowPattern");

        EnsureForeground(target);
        pattern.SetWindowVisualState(WindowVisualState.Maximized);
        Program.Log($"maximize_window: maximized '{title}'");

        return new JsonObject
        {
            ["maximized"] = true,
            ["window_title"] = title
        };
    }

    public static JsonNode MinimizeWindow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var target = ResolveTargetWindow(@params, automation, mainWindow);
        var title = SafeGetWindowTitle(target);

        if (!target.Patterns.Window.TryGetPattern(out var pattern))
            throw new InvalidOperationException("Element does not support WindowPattern");

        pattern.SetWindowVisualState(WindowVisualState.Minimized);
        Program.Log($"minimize_window: minimized '{title}'");

        return new JsonObject
        {
            ["minimized"] = true,
            ["window_title"] = title
        };
    }

    public static JsonNode RestoreWindow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var target = ResolveTargetWindow(@params, automation, mainWindow);
        var title = SafeGetWindowTitle(target);

        if (!target.Patterns.Window.TryGetPattern(out var pattern))
            throw new InvalidOperationException("Element does not support WindowPattern");

        EnsureForeground(target);
        pattern.SetWindowVisualState(WindowVisualState.Normal);
        Program.Log($"restore_window: restored '{title}'");

        return new JsonObject
        {
            ["restored"] = true,
            ["window_title"] = title
        };
    }
}
