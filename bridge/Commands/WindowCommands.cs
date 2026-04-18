using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class WindowCommands
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);

    // SW_SHOW: show window at current size/position without changing state.
    // SW_RESTORE: restore a minimized window to its previous size/position.
    // We use IsIconic to select the appropriate flag so maximized windows are
    // not accidentally restored (un-maximized) when we just want focus.
    private const int SW_SHOW = 5;
    private const int SW_RESTORE = 9;

    internal static void EnsureForeground(AutomationElement? window)
    {
        if (window is null)
            return;

        var hwnd = window.Properties.NativeWindowHandle.ValueOrDefault;
        if (hwnd == IntPtr.Zero)
            return;

        // Only use SW_RESTORE for minimized windows; otherwise SW_SHOW avoids
        // un-maximizing a window that is already maximized.
        var showCmd = IsIconic(hwnd) ? SW_RESTORE : SW_SHOW;
        if (!ShowWindow(hwnd, showCmd))
            Program.Log($"EnsureForeground: ShowWindow(cmd={showCmd}) failed, error={Marshal.GetLastWin32Error()}");

        if (!SetForegroundWindow(hwnd))
            Program.Log($"EnsureForeground: SetForegroundWindow failed, error={Marshal.GetLastWin32Error()}");
    }

    public static JsonNode CloseWindow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var target = WindowResolver.Resolve(@params, automation, mainWindow);
        var title = WindowResolver.SafeGetTitle(target);

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
        var target = WindowResolver.Resolve(@params, automation, mainWindow);
        var title = WindowResolver.SafeGetTitle(target);

        if (!target.Patterns.Window.TryGetPattern(out var pattern))
            throw new InvalidOperationException("Element does not support WindowPattern");

        // Guard: reject if window cannot be maximized or is in a busy state
        var canMaximize = pattern.CanMaximize.ValueOrDefault;
        if (!canMaximize)
        {
            Program.Log($"maximize_window: '{title}' cannot be maximized (CanMaximize=false)");
            return new JsonObject
            {
                ["maximized"] = false,
                ["reason"] = "window cannot be maximized",
                ["window_title"] = title
            };
        }

        var interactionState = pattern.WindowInteractionState.ValueOrDefault;
        if (interactionState == WindowInteractionState.NotResponding
            || interactionState == WindowInteractionState.BlockedByModalWindow)
        {
            Program.Log($"maximize_window: '{title}' is busy (state={interactionState})");
            return new JsonObject
            {
                ["maximized"] = false,
                ["reason"] = "window busy",
                ["window_title"] = title
            };
        }

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
        var target = WindowResolver.Resolve(@params, automation, mainWindow);
        var title = WindowResolver.SafeGetTitle(target);

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
        var target = WindowResolver.Resolve(@params, automation, mainWindow);
        var title = WindowResolver.SafeGetTitle(target);

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
