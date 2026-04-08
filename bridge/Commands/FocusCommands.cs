using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

/// <summary>
/// UIA-based focus commands that work regardless of monitor position or DPI.
/// Uses AutomationElement.Focus() + Win32 SetForegroundWindow instead of
/// coordinate-based mouse clicks.
/// </summary>
public static class FocusCommands
{
    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    private const int SW_RESTORE = 9;

    /// <summary>
    /// Set focus to an element using UIA Focus() — monitor/DPI-agnostic.
    /// Also brings the parent window to foreground via Win32.
    /// </summary>
    public static JsonNode SetFocus(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var automationId = @params?["automationId"]?.GetValue<string>();
        var name = @params?["name"]?.GetValue<string>();

        AutomationElement? element = null;
        var cf = new ConditionFactory(automation.PropertyLibrary);

        if (automationId is not null)
        {
            element = mainWindow.FindFirstDescendant(cf.ByAutomationId(automationId));
        }
        else if (name is not null)
        {
            element = mainWindow.FindFirstDescendant(cf.ByName(name));
        }

        // Step 1: Bring window to foreground via Win32
        var hwnd = mainWindow.Properties.NativeWindowHandle.ValueOrDefault;
        if (hwnd != IntPtr.Zero)
        {
            ShowWindow(hwnd, SW_RESTORE);
            SetForegroundWindow(hwnd);
        }

        // Step 2: Set UIA focus on the target element (or window if no element specified)
        if (element is not null)
        {
            element.Focus();
            return new JsonObject
            {
                ["focused"] = true,
                ["automationId"] = automationId,
                ["name"] = name,
                ["method"] = "UIA.Focus"
            };
        }
        else if (automationId is null && name is null)
        {
            // Just bring window to foreground
            mainWindow.Focus();
            return new JsonObject
            {
                ["focused"] = true,
                ["method"] = "Window.Focus"
            };
        }
        else
        {
            throw new InvalidOperationException(
                $"Element not found: automationId={automationId}, name={name}");
        }
    }
}
